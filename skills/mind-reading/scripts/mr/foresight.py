"""Prospective forecasts with later human outcomes, kept separate from replay.

The journal freezes what was predicted before an observed follow-up. It does
not infer success from silence or turn hindsight into an earlier prediction.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import uuid

from .preferences import PreferenceError, _safe_path
from .select import files_hash
from .config import read_jsonl


class ForesightError(ValueError):
    pass


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def _iso(value):
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _time(value):
    if not isinstance(value, str):
        raise ForesightError("observation time must be an ISO timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForesightError("invalid observation timestamp") from exc
    if parsed.tzinfo is None:
        raise ForesightError("observation time needs a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _source(value):
    return (isinstance(value, str) and 0 < len(value.strip()) <= 2000) or (
        isinstance(value, dict) and set(value) == {"source", "session", "turn"}
        and all(isinstance(v, str) and 0 < len(v.strip()) <= 1000 for v in value.values()))


def _hash(row):
    return hashlib.sha256(json.dumps({k: v for k, v in row.items() if k != "prediction_hash"},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _path(paths, name):
    try:
        return _safe_path(paths.home / "telemetry" / name)
    except PreferenceError as exc:
        raise ForesightError(str(exc)) from exc


def _append(paths, name, row):
    path = _path(paths, name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (json.dumps(row, ensure_ascii=False) + "\n").encode()
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:
            os.chmod(path, 0o600)
        if os.fstat(fd).st_size:
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b"\n":
                raw = b"\n" + raw  # keep a failed prior append from swallowing this row
        remaining = memoryview(raw)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise ForesightError("forecast journal write was incomplete")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _rows(paths, name):
    path = _path(paths, name)
    if not path.exists():
        return [], 0
    rows, invalid = [], 0
    for raw in path.read_bytes().splitlines():
        try:
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("not an object")
            rows.append(value)
        except ValueError:
            invalid += 1
    return rows, invalid


def _forecasts(paths):
    rows, invalid = _rows(paths, "forecasts.jsonl")
    valid = {}
    for row in rows:
        try:
            if (row.get("prediction_hash") != _hash(row) or not isinstance(row.get("forecast_id"), str)
                    or not _source(row.get("source_ref")) or not isinstance(row.get("predictions"), list)
                    or row["forecast_id"] in valid):
                raise ValueError("invalid forecast")
            _time(row.get("created_at"))
            ids = [p["id"] for p in row["predictions"]]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate predictions")
            valid[row["forecast_id"]] = row
        except (ValueError, KeyError, TypeError):
            invalid += 1
    return valid, invalid


def record(paths, data):
    if (not isinstance(data, dict) or set(data) - {"seed", "source_ref", "predictions", "decision_id"}
            or not isinstance(data.get("seed"), str) or not data["seed"].strip() or not _source(data.get("source_ref"))):
        raise ForesightError("record needs seed, source_ref and predictions; no later turns or caller timestamp")
    if "decision_id" in data:
        decisions = {r.get("decision_id"): r for r in read_jsonl(paths.decisions) if isinstance(r, dict)}
        if not isinstance(data["decision_id"], str) or data["decision_id"] not in decisions:
            raise ForesightError("decision_id must name an existing recorded invocation")
        if decisions[data["decision_id"]].get("model_hash") != files_hash(paths.model):
            raise ForesightError("linked decision used a different or unknown model; keep the source binding exact")
    predictions = data.get("predictions")
    if not isinstance(predictions, list) or len(predictions) > 8:
        raise ForesightError("predictions must be a list with at most eight entries; empty means abstain")
    identity = "f-" + uuid.uuid4().hex
    clean = []
    for i, prediction in enumerate(predictions):
        if (not isinstance(prediction, dict) or set(prediction) - {"text", "support", "confidence"}
                or not isinstance(prediction.get("text"), str) or not 1 <= len(prediction["text"].strip()) <= 1000
                or not isinstance(prediction.get("support", []), list)
                or any(not isinstance(s, str) or len(s) > 1000 for s in prediction.get("support", []))
                or prediction.get("confidence", "G") not in ("S", "L", "G")):
            raise ForesightError("each prediction needs text and optional source support and S/L/G confidence")
        clean.append({"id": identity + "-" + str(i + 1), "text": prediction["text"].strip(),
                      "support": prediction.get("support", []), "confidence": prediction.get("confidence", "G")})
    runtime = Path(__file__).resolve().parents[2] / "SKILL.md"
    row = {"forecast_id": identity, "created_at": _iso(now_utc()), "source_ref": data["source_ref"],
           "seed_hash": hashlib.sha256(data["seed"].encode()).hexdigest()[:16],
           "model_hash": files_hash(paths.model), "runtime_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
           "predictions": clean}
    if data.get("decision_id"):
        row["decision_id"] = data["decision_id"]
    row["prediction_hash"] = _hash(row)
    _append(paths, "forecasts.jsonl", row)
    return row


def _validate_observation(data, forecast, current):
    if data.get("reviewer") != "human" or not _source(data.get("source_ref")):
        raise ForesightError("observations need an explicit human reviewer and actual later source_ref")
    observed = _time(data.get("observed_at"))
    if not _time(forecast["created_at"]) < observed <= current or data["source_ref"] == forecast["source_ref"]:
        raise ForesightError("the observed source must be after the registered forecast and no later than now")
    labels, seen = data.get("labels"), set()
    allowed = {p["id"] for p in forecast["predictions"]}
    if not isinstance(labels, list) or not labels:
        raise ForesightError("observations need explicit prediction labels")
    for label in labels:
        if (not isinstance(label, dict) or set(label) != {"prediction_id", "outcome"}
                or not isinstance(label["prediction_id"], str) or label["prediction_id"] not in allowed or label["prediction_id"] in seen
                or label["outcome"] not in ("wanted", "unwanted", "unresolved")):
            raise ForesightError("invalid, unknown or duplicate prediction label")
        seen.add(label["prediction_id"])


def observe(paths, data):
    if not isinstance(data, dict) or not isinstance(data.get("forecast_id"), str):
        raise ForesightError("observation must be an object")
    forecasts, _ = _forecasts(paths)
    forecast = forecasts.get(data.get("forecast_id"))
    if forecast is None:
        raise ForesightError("unknown or altered forecast")
    _validate_observation(data, forecast, now_utc())
    row = {k: data[k] for k in ("forecast_id", "reviewer", "source_ref", "observed_at", "labels")}
    row.update(prediction_hash=forecast["prediction_hash"], reviewed_at=_iso(now_utc()))
    _append(paths, "forecast-outcomes.jsonl", row)
    return row


def report(paths):
    forecasts, invalid = _forecasts(paths)
    observations, bad_outcomes = _rows(paths, "forecast-outcomes.jsonl")
    outcomes = {}
    for row in observations:
        try:
            forecast = forecasts[row["forecast_id"]]
            if row.get("prediction_hash") != forecast["prediction_hash"]:
                raise ForesightError("forecast changed")
            _validate_observation(row, forecast, now_utc())
            for label in row["labels"]:
                outcomes[label["prediction_id"]] = label["outcome"]
        except (ValueError, KeyError, TypeError):
            bad_outcomes += 1
    wanted = sum(v == "wanted" for v in outcomes.values())
    unwanted = sum(v == "unwanted" for v in outcomes.values())
    total = sum(len(r["predictions"]) for r in forecasts.values())
    return {"forecasts": len(forecasts), "predictions": total, "pending_predictions": total - len(outcomes),
            "abstentions": sum(not r["predictions"] for r in forecasts.values()),
            "scored_predictions": wanted + unwanted, "wanted": wanted, "unwanted": unwanted,
            "unresolved_predictions": sum(v == "unresolved" for v in outcomes.values()),
            "wanted_per_100_scored_predictions": round(100 * wanted / (wanted + unwanted), 1) if wanted + unwanted else None,
            "invalid_forecasts": invalid, "invalid_observations": bad_outcomes,
            "limitation": "Precision of explicit human labels on actual later turns; pending and unresolved are unknown. Recall is not measured."}
