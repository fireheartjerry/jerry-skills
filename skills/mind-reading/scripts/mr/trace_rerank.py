"""Optional local cross-encoder style reranker for the head of a retrieval list.

Scores (query prefix, past prefix) pairs with a local causal LM by the next-token yes/no probability. Offline only:
nothing here downloads, and a missing model directory is an error, not a fetch. It never sees historical targets; the
caller passes rendered input prefixes. torch and transformers are imported lazily so the stdlib-only skill still loads.
"""
import math
import os
from pathlib import Path

TOP_K = 12
BATCH = 8
INSTRUCT = "Retrieve the most relevant past interaction prefix to help understand the current request."
SYSTEM_PREFIX = ("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the "
                 "Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n")
ASSISTANT_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def format_pair(query, document, instruct=INSTRUCT):
    """The exact yes/no judging prompt for one (query, document) pair."""
    return "%s<Instruct>: %s\n<Query>: %s\n<Document>: %s%s" % (SYSTEM_PREFIX, instruct, query, document,
                                                                ASSISTANT_SUFFIX)


def tokenizer_kwargs():
    return {"local_files_only": True, "padding_side": "left"}


def model_kwargs(torch_dtype):
    return {"local_files_only": True, "torch_dtype": torch_dtype}


class LocalReranker:
    def __init__(self, path, device="cpu", max_length=512):
        if type(max_length) is not int or max_length < 64:
            raise ValueError("max_length must be an integer >= 64")
        resolved = Path(path).resolve()
        if not resolved.is_dir():
            raise ValueError("local reranker weights missing; downloads must be explicit")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self._torch = torch
        self.device = device
        self.max_length = max_length
        self.tokenizer = AutoTokenizer.from_pretrained(str(resolved), **tokenizer_kwargs())
        dtype = torch.float16 if device != "cpu" else torch.float32
        self.model = AutoModelForCausalLM.from_pretrained(str(resolved), **model_kwargs(dtype)).to(device).eval()
        self.no_id = self.tokenizer.convert_tokens_to_ids("no")
        self.yes_id = self.tokenizer.convert_tokens_to_ids("yes")
        unknown = self.tokenizer.unk_token_id
        if not isinstance(self.no_id, int) or not isinstance(self.yes_id, int) or unknown in (self.no_id, self.yes_id):
            raise ValueError("reranker tokenizer has no yes/no tokens")

    def _clip(self, text, tokens):
        ids = self.tokenizer(text, add_special_tokens=False)["input_ids"]
        return text if len(ids) <= tokens else self.tokenizer.decode(ids[:tokens])

    def scores(self, query, documents):
        """Probability-like yes score in [0, 1] per document, in input order."""
        if not documents:
            return []
        query = self._clip(query, self.max_length // 3)
        fixed = len(self.tokenizer(format_pair(query, ""), add_special_tokens=False)["input_ids"])
        budget = max(16, self.max_length - fixed)
        texts = [format_pair(query, self._clip(doc, budget)) for doc in documents]
        out = []
        for start in range(0, len(texts), BATCH):
            batch = self.tokenizer(texts[start:start + BATCH], padding=True, truncation=True,
                                   max_length=self.max_length, return_tensors="pt").to(self.device)
            with self._torch.no_grad():
                logits = self.model(**batch).logits[:, -1, :]
                pair = logits[:, [self.no_id, self.yes_id]].float()
                out.extend(float(v) for v in self._torch.softmax(pair, dim=1)[:, 1].cpu().tolist())
        return out


def rerank_pairs(pairs, doc_text, query, scorer, top_k=TOP_K, skip=None):
    """Rescore the top_k eligible (index, score) pairs with scorer.scores(query, docs); keep the rest as a tail.

    Head order is by the base score (ties by index) so the choice is deterministic. Head scores become the reranker
    scores. Tail scores are scaled to sit at or below the lowest head score, preserving their order. `skip(index)`
    marks pairs the packet will drop anyway (for example the current session); they never use a rerank slot.
    """
    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    ordered = sorted(pairs, key=lambda p: (-round(p[1], 9), p[0]))
    head = [p for p in ordered if not (skip and skip(p[0]))][:top_k]
    if not head:
        return list(ordered)
    scored = scorer.scores(query, [doc_text(i) for i, _ in head])
    if len(scored) != len(head) or any(isinstance(s, bool) or not isinstance(s, (int, float)) or not math.isfinite(s)
                                       or not 0.0 <= s <= 1.0 for s in scored):
        raise ValueError("reranker must return one finite score in [0, 1] per document")
    in_head = {i for i, _ in head}
    floor = min(scored)
    tail = [(i, min(s, 1.0) * floor) for i, s in ordered if i not in in_head]
    return [(i, float(s)) for (i, _), s in zip(head, scored)] + tail
