"""Standing grants: explicit, verbatim authorizations the user typed, kept apart from inferred preferences.

A grant is admitted only when the user's own typed text carries an explicit grant phrase (GRANT_PHRASES) in a sentence
that is neither negated earlier ("never push without asking me") nor hypothetical (a question, "what if", "would you").
Nothing is inferred from behaviour or silence. `synthesize grants` builds grants.md deterministically from mined
candidates: each candidate is located in a typed corpus record, the sentence that holds the grant phrase is checked
and becomes the quote, the record's id becomes the grant's source, and the engine's Covers is kept only when the quote
names every action word in it (otherwise Covers is derived from the quote itself). The user edits the file by hand
afterwards (revoke, narrow, delete, add).

Two classes of grant:

- `action` (the default): the quote names a class of action ("push the working branch whenever its green") and Covers
  is limited to what the quote names. Covers never names permission, sandbox, security, hook, webhook, forwarding,
  configuration or credential changes; a spend grant states a cap; a message grant names the person or channel.
- `autonomy`: the quote grants general autonomy ("you have max permissions", "you're pre-authorized", "ur in bypass
  perms", "do it in bypass permissions mode") rather than one action. The bypass family counts only in such a
  second-person frame: a bare mention ("bypassing permissions is bad"), a description of the mode ("the bypass
  permissions setting is off"), a request to change it ("turn on bypass permissions in settings.json") or an
  instruction about another process ("run claude with --dangerously-skip-permissions") is not a grant. Its Covers is
  the fixed text AUTONOMY_COVERS and its Scope AUTONOMY_SCOPE. At runtime it lifts the hold only on reversible,
  own-repo actions and on guesses that would otherwise be held. It never covers outward-facing actions (a message or
  post needs a grant that names the person or channel, or the user's go), destructive actions, spend, or permission,
  sandbox or security configuration unless the user's literal current message asks; the harness and system policy
  still win wherever they forbid something. An exception clause the user typed after the phrase ("except don't push
  to main", "but never message anyone") is kept verbatim at the head of Never covers.

Format (model/grants.md):

    # Alex: standing grants

    <fixed intro paragraph>

    ## G1. Push the working branch when green
    - Quote: "push it when green, no need to ask me first" (2026-01-03, typed, cs01)
    - Source: claude-code:cs01:d5c7155a-39db-59f6-a31d-26d698bf6ae6
    - Scope: Alex's own projects
    - Covers: git push of the working branch after the verify command passes
    - Never covers: main, shared repos, force-push, new PRs, deploys
    - Status: active

    ## G2. Autonomy within the current harness mode
    - Quote: "you have max permissions, except don't push to main" (2026-02-01, typed, cs08)
    - Source: claude-code:cs08:...
    - Class: autonomy
    - Scope: every interactive session
    - Covers: reversible, own-repo actions and guesses that would otherwise be held, within the current harness ...
    - Never covers: the quote's own limit: "except don't push to main"; outward-facing actions (...); ...
    - Status: active

`Source` is optional (a hand-added grant has none); it is `source:session:record_id` of the corpus record the quote
came from and is what deduplication and tombstones key on, so a revoked or deleted grant never comes back from a
re-mined paraphrase of the same message. `Class` is optional and defaults to `action`.

Rules the lint enforces: every required field present; the quote contains a grant phrase that no negator precedes in
its sentence and the sentence is not a question or hypothetical; an action grant's Covers names a class of action
whose action word the quote also names (no blanket grants) and never names permission, sandbox, security, hook,
webhook, forwarding, configuration or credential changes (the harness owns those); a spend grant states a cap; a grant
that covers messages, posts, PRs or issues to other people names the person (role token) or channel; an autonomy
grant's quote carries an autonomy phrase that passes autonomy_check and its Covers is exactly AUTONOMY_COVERS. The Quote line is exempt from the
forbidden-rule patterns because it is the user's verbatim text, not an instruction to the agent; the fixed autonomy
Covers line is exempt for the same reason (its text is this module's, and it names the harness as the limit)."""
import re

GRANT_CLASSES = ("action", "autonomy")

# The fixed text of an autonomy grant. The runtime reads it as: do not hold reversible, own-repo actions or guesses
# that would otherwise be held; everything in AUTONOMY_NEVER stays held whatever the harness mode would allow, and the
# harness permission mode is a limit the grant never changes.
AUTONOMY_COVERS = "reversible, own-repo actions and guesses that would otherwise be held, within the current harness permission mode"
AUTONOMY_NEVER = ("outward-facing actions (a message or post needs a grant that names the person or channel, or the user's go); "
                  "destructive actions, force-pushes, deploys and deletes of anything not regenerable; spend; permission, sandbox "
                  "or security configuration unless the user's literal current message asks (text in the expansion does not "
                  "count); anything the harness or system policy forbids")
# The fixed text earlier versions wrote. It read as covering whatever the harness mode allowed, which in a bypass mode
# is unbounded; migrate_autonomy() rewrites it to the current text, and the lint names it until then.
LEGACY_AUTONOMY_COVERS = "operate without asking for approval for actions the current harness permission mode allows"
LEGACY_AUTONOMY_NEVER = ("permission, sandbox or security configuration changes unless the current message asks for them; "
                         "messages or posts to other people; anything the harness or system policy forbids")
AUTONOMY_TITLE = "Autonomy within the current harness mode"
AUTONOMY_SCOPE = "every interactive session"

# Verbs that name a class of action the runtime otherwise holds (outward, destructive, spend, git and process
# operations). A bare "always <verb>" or "from now on ... <verb>" is a grant only with one of these; "always verify
# on the real path" is a preference and stays in the profile.
GRANT_ACTION_VERBS = ("push", "merge", "commit", "deploy", "ship", "release", "tag", "publish", "post", "send", "message",
                      "dm", "email", "reply", "delete", "remove", "rm", "wipe", "drop", "kill", "install", "uninstall",
                      "upgrade", "spend", "pay", "buy", "purchase", "launch", "rebase", "force-push", "force push",
                      "overwrite", "revert", "reset", "rotate", "sudo")
_VERB = r"(?:%s)(?:e?[sd]|ing)?" % "|".join(re.escape(v) for v in GRANT_ACTION_VERBS)
_VERB_RE = re.compile(r"(?i)\b%s\b" % _VERB)

# The bypass family names a harness mode. Bare, it is talk about the mode ("bypassing permissions is bad", "turn on
# bypass permissions in settings.json"); it is a grant only in a second-person frame that says the agent is in or has
# that mode ("ur in bypass perms", "you have bypass permissions") or tells it to work in it ("do it in bypass
# permissions mode"). `perms` is the user's own shorthand for permissions.
_BYPASS_CORE = (r"(?:(?:bypass(?:ing|ed)?|skip(?:ping)?)[ -](?:all |the |every )?(?:permissions?|perms)(?: mode| prompts?| checks?)?"
                r"|(?:permissions?|perms)[- ]bypass(?: mode)?|--dangerously-skip-permissions|dangerously[- ]skip[- ]permissions"
                r"|yolo mode)")
_YOU = r"(?:you'?re|youre|you are|u r|u are|ur|you|u)"
_BYPASS_FRAMED = [
    r"(?i)\b%s (?:(?:now|already|still|literally|legit|basically|just|currently) )?(?:in|on|under|using|running (?:in|on|with)|"
    r"have|got|'ve got|have got) (?:the |full |max )?%s" % (_YOU, _BYPASS_CORE),
    r"(?i)\b(?:go|do it|do this|do that|do everything|do all of (?:it|this|that)|work|proceed|operate|continue|keep going) in "
    r"(?:the )?%s" % _BYPASS_CORE,
]
_PREAUTH = (r"(?i)\b(?:%s|consider yourself|treat yourself as|i'?ve|i have|i) (?:(?:already|now|fully|hereby|totally) )?"
            r"pre[- ]?authori[sz](?:ed|e you)\b" % _YOU)
_YOU_HAVE_PERMS = (r"(?i)\b(?:you|u) (?:have|'ve got|got|legit have|do have|now have) (?:my )?(?:full |max |maximum |complete |total |"
                   r"all |every |blanket )?(?:permissions?|perms|approval|autonomy|authority|the go[- ]ahead|a standing)\b")

# Each entry: (pattern, strip, check). `strip` marks the asking or permission language a derived Covers drops
# (conditions such as "whenever its green" stay, because they bound the class of action). `check` is None or:
# "verb", a standing marker that is a grant only when the sentence also names an action verb from GRANT_ACTION_VERBS;
# "always", an "always <verb>" that is not a grant after "can"/"could"/"may" or beside doubt ("I'm not sure you can
# always push"); "autonomy", a phrase that is not a grant when a state word or a negator follows it in its sentence
# ("the bypass permissions setting is off"), when an imperative config verb precedes it ("turn on ..."), or when it
# sits in a condition ("if you have full perms").
GRANT_PHRASE_SPECS = [
    (r"(?i)\b(?:you )?(?:don'?t|do not|dont) (?:need to |have to |gotta |ever )?ask(?: me)?(?: first| again| before| for| about| to)?\b", True, None),
    (r"(?i)\bno need to (?:ask|confirm|check(?: in| with me)?)(?: me)?(?: first| again| before| about| for)?\b", True, None),
    (r"(?i)\bwithout asking(?: me)?(?: first)?\b", True, None),
    (_YOU_HAVE_PERMS, True, "autonomy"),
    (r"(?i)\b(?:you are|you'?re|u r|u are|ur) (?:cleared|authori[sz]ed|approved) to\b", True, None),
    (r"(?i)\balways (?:ok|okay|fine|allowed|approved|free) to\b", True, None),
    (r"(?i)\bstanding (?:permission|approval|authori[sz]ation|ok|yes|grant)\b", True, None),
    (r"(?i)\bi (?:authori[sz]e|approve|permit|allow|pre[- ]?authori[sz]e) you to\b", True, None),
    (_PREAUTH, True, "autonomy"),
] + [(p, True, "autonomy") for p in _BYPASS_FRAMED] + [
    (r"(?i)\bgo ahead (?:and|with)\b(?=.{0,60}\b(?:whenever|any ?time|always|from now on)\b)", True, None),
    (r"(?i)\bfrom now on\b(?=.{0,80}\b(?:just|always|go ahead|do it|you can|no need)\b)", True, None),
    (r"(?i)\bfrom now on\b", True, "verb"),
    (r"(?i)\balways(?= (?:just |also |immediately |directly )?%s\b)" % _VERB, True, "always"),
    (r"(?i)\bwhenever (?:you need|needed|necessary|it'?s? (?:green|ready|done)|its (?:green|ready|done))\b", False, None),
    (r"(?i)\bnever ask (?:me )?(?:before|about|for)\b", True, None),
    (r"(?i)\bnever (?:need|have) to ask\b", True, None),
    (r"(?i)\bno confirmation needed\b", True, None),
    # the match is the opener only, so a derived Covers keeps the action and its condition
    (r"(?i)\byou (?:can|may) (?:just )?(?=(?:push|merge|commit|deploy|spend|delete|send|run|install)\b.{0,80}\b(?:without|any ?time|whenever|no need|always)\b)", True, None),
]
GRANT_PHRASES = [p for p, _, _ in GRANT_PHRASE_SPECS]
# Phrases that grant general autonomy rather than one action: such a quote becomes a `Class: autonomy` grant. A
# permission phrase followed by "to <verb>" names an action and stays an action grant. Every one passes the
# "autonomy" check above before it counts.
AUTONOMY_PHRASES = list(_BYPASS_FRAMED) + [
    r"(?i)\b(?:%s|consider yourself|treat yourself as|i'?ve|i have|i) (?:(?:already|now|fully|hereby|totally) )?"
    r"pre[- ]?authori[sz](?:ed|e you)\b(?!\s+(?:you\s+)?to\s+\w)" % _YOU,
    r"(?i)\b(?:you|u) (?:have|'ve got|got|legit have|do have|now have) (?:my )?(?:full|max|maximum|complete|total|all|every|blanket) "
    r"(?:permissions?|perms|autonomy|authority|control)\b(?!\s+to\s+\w)",
    r"(?i)\b(?:full|max(?:imum)?|complete|total|blanket) (?:autonomy|permissions|perms)\b(?!\s+to\s+\w)",
]
# Weak openers: a one-off approval or a pleasantry on their own ("feel free to run that", "you're allowed to delete
# that one"). They count only when a standing marker (STANDING_MARKERS) sits elsewhere in the same text: "from now on
# feel free to restart the worker", "in general you're allowed to restart the worker".
WEAK_OPENERS = [
    r"(?i)\bfeel free to\b",
    r"(?i)\byou can always\b",
    r"(?i)\bwhenever you want\b",
    r"(?i)\b(?:you are|you'?re|u r|u are|ur) (?:allowed|free|permitted|welcome) to\b",
]
STANDING_MARKERS = r"(?i)\b(?:always|from now on|any ?time|anytime|in general|every time|going forward|by default|as a (?:rule|standing rule))\b"
_GRANT_RE = [(re.compile(p), strip, check) for p, strip, check in GRANT_PHRASE_SPECS]
_AUTONOMY_RE = [re.compile(p) for p in AUTONOMY_PHRASES]
_WEAK_RE = [re.compile(p) for p in WEAK_OPENERS]
_ASK_RE = [r for r, strip, _ in _GRANT_RE if strip] + _WEAK_RE
_MARKER_RE = re.compile(STANDING_MARKERS)

# A negator earlier in the sentence turns a grant phrase into a prohibition: "never push to main without asking me".
NEGATORS = r"(?i)\b(?:never|don'?t|dont|do not|not|no|can'?t|cannot|shouldn'?t|won'?t|mustn'?t|stop|avoid|nor|neither)\b"
_NEGATOR_RE = re.compile(NEGATORS)
# "no worries, you can push whenever green" and "no, you don't need to ask" are not prohibitions.
_NOT_NEGATION_RE = re.compile(r"(?i)\bno (?:worries|rush|problem|biggie|stress|pressure|hurry)\b|\bnot (?:a problem|an issue)\b|^\s*(?:no|nah|nope)\s*[,:;.!-]+\s*")
# A question or a hypothetical is talk about a grant, not a grant: "what if you could push without asking?".
HYPOTHETICAL = (r"(?i)\b(?:what if|would (?:you|it|that|i)|should i|shall i|could i|can i|may i|suppose|supposing|imagine|"
                r"hypothetically|in theory|i wonder|if i (?:said|told|gave|let|allowed|were)|if you were|pretend|say i|"
                r"do you (?:need|want|think)|are you (?:allowed|able|free|cleared))\b")
_HYPOTHETICAL_RE = re.compile(HYPOTHETICAL)
# A period ends a sentence only before whitespace or the end of the text, so "settings.json", "e.g" inside a word
# and version numbers such as 5.1 do not cut a quote short.
_SENTENCE_SPLIT = re.compile(r"\.+(?=\s|$)|[!?;\n]+")
# After an autonomy phrase, in its sentence: an exception clause the user typed ("except don't push to main", "but never
# message anyone") narrows the grant and is kept in Never covers; a state word or negator ("is off", "doesn't let
# you") means the sentence describes the mode rather than granting it.
_EXCEPTION_RE = re.compile(r"(?i)\b(?:except|but|unless|other than|apart from|besides|as long as|only (?:if|when))\b"
                           r"|\b(?:never|don'?t|dont|do not)\b(?! (?:need to |have to |ever )?(?:ask|wait|check|confirm|bother|prompt))")
_ASK_REINFORCE_RE = re.compile(r"(?i)\b(?:don'?t|dont|do not|never|no need to) (?:need to |have to |ever )?(?:ask|wait|check|confirm|bother|prompt)\w*")
_POST_STATE_RE = re.compile(r"(?i)\b(?:off|disabled|not|isn'?t|aren'?t|wasn'?t|weren'?t|doesn'?t|does not|won'?t|will not|can'?t|cannot|"
                            r"bad|dangerous|risky|unsafe|broken)\b")
_CONFIG_VERB_RE = re.compile(r"(?i)\b(?:turn(?:ing)? (?:on|it on)|turn on|enabl(?:e|es|ed|ing)|set(?:ting)?|switch(?:ing)? (?:to|on|into)|"
                             r"put (?:me|it|you|yourself|claude|codex) (?:in|into|on)|toggl(?:e|ing)|configur(?:e|ing))\b")
_CONDITION_BEFORE_RE = re.compile(r"(?i)\b(?:if|when|once|whether|unless|until)\s+(?:[\w']+\s+){0,3}$|\b(?:if|when|once|whether|unless|until)\s*$")
_DOUBT_RE = re.compile(r"(?i)\b(?:not sure|unsure|i doubt|wonder(?:ing)?|idk|don'?t know|no idea)\b")
_CAN_BEFORE_RE = re.compile(r"(?i)\b(?:can|could|may|might|cannot|can'?t)\s*$")

# An action grant can never cover these: the harness owns them, and a blanket grant names no class of action.
NEVER_GRANTABLE = [
    (r"(?i)\b(permissions?|sandbox|security|allow[- ]?list|auto[- ]?approv\w*|\w*hooks?|settings?\.(?:json|toml|yaml|yml)|"
     r"config(?:uration)?|config\.(?:toml|json|yaml|yml)|\.codex|\.claude|CLAUDE\.md|AGENTS\.md|forwarding|auto[- ]?repl(?:y|ies)|"
     r"bypass\w*|dangerously)\b",
     "permission, sandbox, security or configuration changes are the harness's to approve, never a grant's"),
    (r"(?i)\b(credentials?|passwords?|api[- ]?keys?|tokens?|secrets?)\b",
     "credential handling cannot be granted"),
]
_NEVER_RE = [(re.compile(p), why) for p, why in NEVER_GRANTABLE]
BLANKET_REASON = "a blanket grant names no class of action; write what it covers"
_BLANKET_RE = re.compile(r"(?i)\b(anything|everything|whatever|all actions|any action|full (?:perms|permissions|autonomy|control))\b")
# A Covers whose first content word is one of these names no action of its own: "do anything you need" is a blanket,
# "push everything" is a push grant with a broad object.
_GENERIC_HEADS = {"anything", "everything", "whatever", "all", "any", "full", "do", "doing", "handle", "act", "take", "go",
                  "proceed", "work", "perform", "complete", "finish"}
_MESSAGE_RE = re.compile(r"(?i)\b(messages?|messaging|dm|dms|texts?|texting|e-?mails?|repl(?:y|ies)|posts?|posting|send|sending|"
                         r"slack|discord|imessage|whatsapp|telegram|tweets?|comments?|publish|ping|pings?|notify|notifies|"
                         r"notifications?|contact|invite|invites?|announce|outreach|prs?|pull requests?|"
                         r"open(?:ing)? (?:an? )?(?:issue|ticket)s?|file (?:an? )?(?:issue|ticket)s?)\b")
# A recipient is a role token from this list (plus the people tokens from config) or a #channel / @handle.
ROLE_TOKENS = ("<teammate>", "<stakeholder>", "<repo-owner>", "<vendor>", "<friend>", "<family>")
_CHANNEL_RE = re.compile(r"(?<![\w/])[#@][A-Za-z0-9_-]{2,}")
_SPEND_RE = re.compile(r"(?i)\b(spend|spending|pay|paying|purchase|buy|buying|bill|billing|cost|costs|budget|usd|dollars?|credits?)\b|\$\s?\d")
_CAP_RE = re.compile(r"(?i)\$\s?\d|\d+\s?(?:usd|dollars?|cad|eur|gbp|credits?)\b|\b(?:up to|cap|capped|ceiling|limit|limited|max|maximum|under|below|at most|no more than)\b")
_HEAD_RE = re.compile(r"^## G(\d+)\. (.+?)\s*$")
_FIELD_RE = re.compile(r"^- (Quote|Source|Class|Scope|Covers|Never covers|Status)\s*:\s*(.*?)\s*$")
_QUOTE_RE = re.compile(r'^"(.*)"\s*\((\d{4}-\d{2}-\d{2})(?:,\s*([a-z_]+))?(?:,\s*([^)]*))?\)\s*$')
_STATUS_RE = re.compile(r"^(active|revoked(?: \d{4}-\d{2}-\d{2})?)$")
FIELDS = ("Quote", "Scope", "Covers", "Never covers", "Status")
_WS = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9][a-z0-9'+-]*")
# Words a Covers line may carry without the quote naming them: grammar, time, one-off objects and the user's own-project
# framing. "delete that one" names no class of action; "delete regenerable things" does.
_COVERS_FREE = {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "at", "by", "as", "so", "up", "it", "its", "it's",
                "is", "are", "be", "been", "that", "which", "with", "into", "from", "when", "once", "after", "before", "then",
                "only", "if", "any", "all", "own", "my", "your", "their", "own", "this", "these", "those", "via", "per", "each",
                "every", "same", "new", "old", "etc", "i", "you", "me", "we", "us", "they", "them", "do", "does", "doing",
                "work", "working", "task", "tasks", "class", "action", "actions", "one", "here", "there", "now", "again", "too",
                "please", "pls", "just", "also", "fully", "completely", "entirely", "simply", "really", "directly", "immediately"}
_FILLER_LEAD = re.compile(r"(?i)^(?:(?:btw|also|ok|okay|and|so|then|yeah|yes|yep|sure|please|pls|just|to|u|you|can|may|could|should|go ahead and|"
                          r"it'?s|its|fine to|ok to|okay to|allowed to|free to|feel free to|from now on|always|in general|any ?time|"
                          r"going forward|by default|for|[,:;-])\s+)+")
_NOT_AN_ACTION = {"ask", "asking", "tell", "telling", "check", "checking", "confirm", "let", "disagree", "say", "report", "verify"}
_FILLER_TAIL = re.compile(r"(?i)(?:\s+(?:btw|also|ok|okay|and|so|then|please|pls|lol|tho|though|bro|man|dude|from now on|always|in general|"
                          r"any ?time|going forward|by default|[,:;-]))+$")

INTRO = ("Each entry is something {name} typed, verbatim, that authorizes a class of action in advance. Only typed text "
         "with an explicit grant phrase qualifies, verified against the sentence it came from; nothing here is inferred "
         "from behaviour or silence. The harness's permission, sandbox and safety rules beat every grant. A grant never "
         "covers messages or posts to other people unless it names them. An autonomy grant (Class: autonomy) lifts the "
         "hold only on reversible, own-repo actions and on guesses that would otherwise be held; it never covers "
         "outward-facing or destructive actions, spend, or permission, sandbox or security configuration unless the "
         "user's literal current message asks. Edit by hand: narrow Covers, extend Never "
         "covers, set Status to revoked, or delete the entry; a revoked or deleted entry is never re-added.")


def _norm(text):
    return _WS.sub(" ", (text or "")).strip().lower()


def _fold(text):
    return _WS.sub(" ", (text or "")).strip()


def _sentence_bounds(text, pos):
    """[start, end) of the sentence of `text` that contains offset `pos`."""
    start = 0
    for m in _SENTENCE_SPLIT.finditer(text):
        if m.end() <= pos:
            start = m.end()
        elif m.start() >= pos:
            return start, m.start()
    return start, len(text)


def _sentence_has_verb(text, pos):
    s0, s1 = _sentence_bounds(text, pos)
    return bool(_VERB_RE.search(text[s0:s1]))


def autonomy_check(text, start, end):
    """(ok, exception, reason) for an autonomy or bypass phrase at text[start:end].

    Not ok when an imperative config verb sits just before it ("turn on bypass permissions", "set it so you have
    bypass permissions"), when it sits in a condition ("if you have full perms"), or when a state word or negator
    follows it in its sentence before any exception clause ("the bypass permissions setting is off", "bypass
    permissions doesn't let you push"). Reinforcement about asking ("so dont ask me") is not a negator. `exception` is
    the clause the user typed after the phrase that narrows it ("except don't push to main"), verbatim, or None."""
    s0, s1 = _sentence_bounds(text, start)
    near = text[max(s0, start - 40):start]
    if _CONFIG_VERB_RE.search(near):
        return False, None, "a request to change the harness mode, not a grant (%r)" % _CONFIG_VERB_RE.search(near).group(0)
    if _CONDITION_BEFORE_RE.search(text[s0:start]):
        return False, None, "a condition, not a grant"
    rest = text[end:s1]
    m = _EXCEPTION_RE.search(rest)
    pre, exception = (rest[:m.start()], rest[m.start():].strip(" ,:;-")) if m else (rest, None)
    state = _POST_STATE_RE.search(_ASK_REINFORCE_RE.sub(" ", pre))
    if state:
        return False, None, "the sentence describes the mode (%r follows it), it does not grant it" % state.group(0)
    return True, exception or None, None


def grant_phrase_spans(text):
    """Every (start, end) of a grant phrase in `text`, earliest first. Weak openers count only when a standing marker
    sits elsewhere in the text; a bare standing marker ("from now on") counts only when its sentence names an action
    verb from GRANT_ACTION_VERBS; "always <verb>" does not count after "can" or beside doubt; an autonomy or bypass
    phrase counts only when autonomy_check passes."""
    text = text or ""
    spans = []
    for p, _strip, check in _GRANT_RE:
        for m in p.finditer(text):
            if check == "verb" and not _sentence_has_verb(text, m.start()):
                continue
            if check == "always":
                s0, _ = _sentence_bounds(text, m.start())
                before = text[s0:m.start()]
                if _CAN_BEFORE_RE.search(before) or _DOUBT_RE.search(before):
                    continue
            if check == "autonomy" and not autonomy_check(text, m.start(), m.end())[0]:
                continue
            spans.append(m.span())
    for p in _WEAK_RE:
        for m in p.finditer(text):
            rest = text[:m.start()] + " " + text[m.end():]
            if _MARKER_RE.search(rest):
                spans.append(m.span())
    return sorted(set(spans))


def has_grant_phrase(text):
    return bool(grant_phrase_spans(text))


def grant_phrase(text):
    """The earliest grant phrase in `text`, or None."""
    spans = grant_phrase_spans(text)
    return (text or "")[spans[0][0]:spans[0][1]] if spans else None


def negation_before(text, phrase_start, sentence_start=None):
    """The negator that precedes the grant phrase at `phrase_start` inside its sentence, or None."""
    if sentence_start is None:
        sentence_start, _ = _sentence_bounds(text, phrase_start)
    before = _NOT_NEGATION_RE.sub(" ", text[sentence_start:phrase_start])
    m = _NEGATOR_RE.search(before)
    return m.group(0) if m else None


def hypothetical(text, pos=None):
    """The hypothetical or question marker in the sentence of `text` at `pos` (whole text when None), or None.
    A sentence that ends with `?` is a question, whatever else it says."""
    folded = _fold(text)
    if pos is None:
        s0, s1 = 0, len(folded)
    else:
        s0, s1 = _sentence_bounds(folded, pos)
    sentence = folded[s0:s1]
    if s1 < len(folded) and folded[s1] == "?":
        return "?"
    if pos is None and sentence.rstrip().endswith("?"):
        return "?"
    m = _HYPOTHETICAL_RE.search(sentence)
    return m.group(0) if m else None


def _autonomy_hit(text):
    """(start, end, exception) of the earliest autonomy phrase in `text` (folded) that no negator precedes, whose
    sentence is not a question or hypothetical, and that passes autonomy_check, or None."""
    folded = _fold(text)
    hits = sorted(m.span() for p in _AUTONOMY_RE for m in p.finditer(folded))
    for a, b in hits:
        if negation_before(folded, a) or hypothetical(folded, a):
            continue
        ok, exception, _ = autonomy_check(folded, a, b)
        if ok:
            return a, b, exception
    return None


def autonomy_phrase(text):
    """The earliest autonomy phrase in `text` whose sentence neither negates nor merely describes it, or None."""
    hit = _autonomy_hit(text)
    return _fold(text)[hit[0]:hit[1]] if hit else None


def autonomy_exception(text):
    """The exception clause the user typed after the autonomy phrase ("except don't push to main"), or None."""
    hit = _autonomy_hit(text)
    return hit[2] if hit else None


def grant_class(text):
    """`autonomy` when the sentence grants general autonomy rather than one action, else `action`."""
    return "autonomy" if autonomy_phrase(text) else "action"


def grant_sentence(text, quote=None):
    """Locate the grant in `text` (a typed record, or the quote itself) and return (sentence, phrase, reason).

    When `quote` is given, the grant phrase is looked for inside the quote's span in `text`; the sentence of `text`
    that holds that phrase is returned verbatim (whitespace folded) so a quote can never cut a negator off. `reason`
    is None on success, otherwise why there is no grant here."""
    folded = _fold(text)
    lower = folded.lower()
    if quote is not None:
        q = _norm(quote)
        pos = lower.find(q) if q else -1
        if pos < 0:
            return None, None, "quote not found verbatim in a typed corpus message"
        window = (pos, pos + len(q))
    else:
        window = (0, len(folded))
    spans = [s for s in grant_phrase_spans(folded) if window[0] <= s[0] and s[1] <= window[1]]
    if not spans and quote is not None:
        # a phrase that straddles the quote's edge still counts when it starts inside the quote
        spans = [s for s in grant_phrase_spans(folded) if window[0] <= s[0] < window[1]]
    if not spans:
        return None, None, "no explicit grant phrase"
    start, end = spans[0]
    s0, s1 = _sentence_bounds(folded, start)
    neg = negation_before(folded, start, s0)
    if neg:
        return None, folded[start:end], "the grant phrase is negated earlier in its sentence (%r)" % neg
    hyp = hypothetical(folded, start)
    if hyp:
        return None, folded[start:end], "the sentence is a question or a hypothetical (%r), not a grant" % hyp
    return folded[s0:s1].strip(" ,:;-"), folded[start:end], None


def negated_grant(text):
    """The negator that cancels the earliest grant phrase in `text`, or None (also None when there is no phrase)."""
    spans = grant_phrase_spans(_fold(text))
    if not spans:
        return None
    return negation_before(_fold(text), spans[0][0])


def _first_content_word(text):
    words = _content_words(text)
    return words[0] if words else ""


def never_grantable(text):
    """The reason an action Covers cannot be granted, or None."""
    for pat, why in _NEVER_RE:
        if pat.search(text or ""):
            return why
    if _BLANKET_RE.search(text or ""):
        head = _first_content_word(text)
        m = _BLANKET_RE.search(text or "")
        if head in _GENERIC_HEADS or head == "" or m.group(0).lower().startswith("full "):
            return BLANKET_REASON
    return None


def spend_without_cap(text):
    """True when `text` covers spending and states no cap (an amount, 'up to', 'ceiling', 'limit', ...)."""
    text = text or ""
    return bool(_SPEND_RE.search(text)) and not _CAP_RE.search(text)


def names_a_recipient(text, tokens=None):
    """True when `text` names a recipient: a role token (ROLE_TOKENS plus any configured people tokens) or a
    #channel / @handle. Any other `<word>` is not a name."""
    text = text or ""
    allowed = set(ROLE_TOKENS) | set(tokens or ())
    low = text.lower()
    if any(t.lower() in low for t in allowed):
        return True
    return bool(_CHANNEL_RE.search(text))


def covers_messages(text):
    return bool(_MESSAGE_RE.search(text or ""))


def _stem(w):
    w = w.lower()
    if len(w) > 5 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 4 and w.endswith("es") and w[-3:-2] in ("s", "x", "z") or w.endswith(("ches", "shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _content_words(text):
    return [w for w in _WORD.findall((text or "").lower()) if w not in _COVERS_FREE and len(w) > 1]


def _word_in(w, quote_stems):
    s = _stem(w)
    if s in quote_stems:
        return True
    return any(len(s) >= 4 and (q.startswith(s) or s.startswith(q)) and min(len(q), len(s)) >= 4 for q in quote_stems)


def covers_named_by_quote(covers, quote, strict=True):
    """strict: every content word of Covers appears in the quote (stemmed; grammar and time words are free).
    lenient: the action word (the first content word of Covers) appears in the quote. A Covers the quote does not
    name was written by the engine or by hand about something the user never said."""
    words = _content_words(covers)
    if not words:
        return False
    quote_stems = {_stem(w) for w in _WORD.findall((quote or "").lower())}
    if strict:
        return all(_word_in(w, quote_stems) for w in words)
    return _word_in(words[0], quote_stems)


def derive_covers(quote):
    """Covers in the user's own words: the quote with its asking and permission phrases and the filler around them
    removed, or None when nothing actionable is left (fewer than two content words, a one-off object such as "that
    one", or an opener that is not an action: "ask", "tell", "disagree")."""
    text = _fold(quote)
    spans = sorted(set(m.span() for p in _ASK_RE for m in p.finditer(text)))
    for a, b in reversed(spans):
        text = text[:a] + " " + text[b:]
    text = _fold(text)
    for _ in range(2):
        text = _FILLER_LEAD.sub("", text)
        text = _FILLER_TAIL.sub("", text).strip(" ,:;.!?-")
    words = _content_words(text)
    if len(words) < 2 or words[0] in _NOT_AN_ACTION:
        return None
    return text[0].lower() + text[1:] if text[:1].isupper() and not text[:2].isupper() else text


def header(name):
    return "# %s: standing grants\n\n%s\n" % (name, INTRO.format(name=name))


def parse_grants(text, path="grants.md", tokens=None):
    """Return (grants, errors). Each grant: id, n, title, quote, date, origin, session, source, cls, scope, covers,
    never, status, active, errors (this grant's own), lines [start, end] (1-based inclusive), raw (the block's lines).
    A block whose heading is malformed is not a grant and is reported only in `errors`."""
    lines = text.splitlines()
    heads = [(i, _HEAD_RE.match(l)) for i, l in enumerate(lines) if l.startswith("## ")]
    grants, errors = [], []
    seen = set()
    for k, (i, m) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        if not m:
            errors.append("%s:%d: grant heading must look like `## G1. Title`" % (path, i + 1))
            continue
        n = int(m.group(1))
        gid = "G%d" % n
        own = []
        if gid in seen:
            own.append("%s:%d: duplicate grant id %s" % (path, i + 1, gid))
        seen.add(gid)
        g = {"id": gid, "n": n, "title": m.group(2).strip(), "lines": [i + 1, end], "raw": lines[i:end], "errors": own,
             "active": False, "source": "", "cls": "action"}
        fields = {}
        for j in range(i + 1, end):
            fm = _FIELD_RE.match(lines[j])
            if fm:
                fields[fm.group(1)] = (fm.group(2), j + 1)
        for f in FIELDS:
            if f not in fields or not fields[f][0]:
                own.append("%s:%d: grant %s has no `- %s:` line" % (path, i + 1, gid, f))
        grants.append(g)
        if any(f not in fields or not fields[f][0] for f in FIELDS):
            errors += own
            continue
        q, qline = fields["Quote"]
        qm = _QUOTE_RE.match(q)
        if not qm:
            own.append("%s:%d: Quote must be `\"verbatim text\" (YYYY-MM-DD, origin, session)`" % (path, qline))
            errors += own
            continue
        cls_raw, cls_line = fields.get("Class") or ("action", i + 1)
        cls = cls_raw.strip().lower() or "action"
        g.update({"quote": qm.group(1), "date": qm.group(2), "origin": qm.group(3) or "typed", "session": (qm.group(4) or "").strip(),
                  "source": (fields.get("Source") or ("", 0))[0].strip(), "cls": cls,
                  "scope": fields["Scope"][0], "covers": fields["Covers"][0], "never": fields["Never covers"][0],
                  "status": fields["Status"][0].strip().lower()})
        if cls not in GRANT_CLASSES:
            own.append("%s:%d: Class must be one of %s" % (path, cls_line, ", ".join(GRANT_CLASSES)))
            cls = "action"
        phrase_ok = has_grant_phrase(g["quote"])
        if not phrase_ok:
            own.append("%s:%d: the quote carries no explicit grant phrase; a preference is not a grant" % (path, qline))
        else:
            neg = negated_grant(g["quote"])
            if neg:
                own.append("%s:%d: the quote negates its grant phrase (%r comes first); a prohibition is not a grant" % (path, qline, neg))
            else:
                hyp = hypothetical(g["quote"], grant_phrase_spans(_fold(g["quote"]))[0][0])
                if hyp:
                    own.append("%s:%d: the quote is a question or a hypothetical (%r), not a grant" % (path, qline, hyp))
        if g["origin"] != "typed":
            own.append("%s:%d: a grant must come from typed text, not %s" % (path, qline, g["origin"]))
        cline = fields["Covers"][1]
        if cls == "autonomy":
            if phrase_ok and not autonomy_phrase(g["quote"]):
                own.append("%s:%d: an autonomy grant's quote must carry an autonomy phrase (bypass permissions, pre-authorized, "
                           "full or max permissions); this quote names one action, so its Class is action" % (path, qline))
            if _norm(g["covers"]) != _norm(AUTONOMY_COVERS):
                legacy = " (this is the older, unbounded text: run `synthesize grants` to migrate it)" if _norm(g["covers"]) == _norm(LEGACY_AUTONOMY_COVERS) else ""
                own.append("%s:%d: an autonomy grant's Covers must be exactly: %s%s" % (path, cline, AUTONOMY_COVERS, legacy))
        else:
            why = never_grantable(g["covers"]) or never_grantable(g["title"])
            if why:
                own.append("%s:%d: Covers cannot be granted: %s" % (path, cline, why))
            if phrase_ok and not covers_named_by_quote(g["covers"], g["quote"], strict=False):
                own.append("%s:%d: Covers names an action the quote does not (its first word must appear in the quote)" % (path, cline))
            if spend_without_cap(g["covers"]):
                own.append("%s:%d: a spend grant must state a cap in Covers (an amount, `up to`, `ceiling`, `limit`)" % (path, cline))
            if covers_messages(g["covers"]) and not (names_a_recipient(g["covers"], tokens) or names_a_recipient(g["title"], tokens)):
                own.append("%s:%d: a grant that covers messages, posts, PRs or issues must name the person (role token) or channel"
                           % (path, cline))
        if not _STATUS_RE.match(g["status"]):
            own.append("%s:%d: Status must be `active` or `revoked YYYY-MM-DD`" % (path, fields["Status"][1]))
        g["active"] = g["status"] == "active"
        errors += own
    counts = {}
    for g in grants:
        counts[g["id"]] = counts.get(g["id"], 0) + 1
    for g in grants:  # a shared id makes `[S:G1]` ambiguous, so every block carrying it is invalid
        if counts[g["id"]] > 1 and not any("duplicate grant id" in e for e in g["errors"]):
            g["errors"].append("%s:%d: duplicate grant id %s" % (path, g["lines"][0], g["id"]))
    return grants, errors


def valid_active_grants(text, tokens=None):
    """Grants that are active and parse with zero errors: the only ones the runtime may apply."""
    grants, _ = parse_grants(text or "", tokens=tokens)
    return [g for g in grants if g.get("active") and not g.get("errors")]


def active_grants(text):
    return valid_active_grants(text)


def _typed_rows(corpus_rows):
    out = []
    for r in corpus_rows:
        if (r.get("origin") or "typed") != "typed" or not r.get("text"):
            continue
        out.append((r, _norm(r.get("text"))))
    return out


def source_id(row):
    return "%s:%s:%s" % (row.get("source") or "?", row.get("session") or "?", row.get("record_id") or row.get("key") or "?")


def verify_quotes(candidates, corpus_rows, tokens=None):
    """Keep candidates whose quote appears verbatim (whitespace-folded, case-insensitive) in a typed corpus record, whose
    grant phrase is neither negated nor hypothetical in the sentence that holds it, and whose Covers is named by the
    quote. Each kept candidate is a copy with `quote` widened to that whole sentence (when it fits 300 characters),
    `source` set to the record's id, `cls` set to its class, and `covers` replaced by a derivation from the quote when
    the engine's Covers named more than the quote does (`covers_derived` True). An autonomy quote gets the fixed
    AUTONOMY_COVERS, AUTONOMY_SCOPE and AUTONOMY_NEVER whatever the engine wrote, with the exception clause the user
    typed after the phrase kept verbatim at the head of Never covers (dropped instead when that clause would read as
    a forbidden rule). Returns (kept, dropped) where dropped carries the reason."""
    typed = _typed_rows(corpus_rows)
    kept, dropped = [], []
    for c in candidates:
        q = _norm(c.get("quote"))
        if (c.get("origin") or "typed") != "typed":
            dropped.append((c, "origin %s is not typed" % c.get("origin")))
            continue
        if not q or not has_grant_phrase(c.get("quote")):
            dropped.append((c, "no explicit grant phrase"))
            continue
        matches = [r for r, t in typed if q in t]
        if not matches:
            dropped.append((c, "quote not found verbatim in a typed corpus message"))
            continue
        same = [r for r in matches if r.get("session") == c.get("session")]
        row = (same or matches)[0]
        sentence, _phrase, reason = grant_sentence(row.get("text"), c.get("quote"))
        if reason:
            dropped.append((c, reason))
            continue
        c = dict(c)
        if len(sentence) <= 300:
            c["quote"] = sentence
        c["source"] = source_id(row)
        c["record_key"] = row.get("key")
        c["session"] = row.get("session") or c.get("session")
        c["time"] = c.get("time") or row.get("time")
        c["cls"] = grant_class(c["quote"])
        if c["cls"] == "autonomy":
            exception = autonomy_exception(c["quote"])
            c["covers_derived"] = _norm(c.get("covers")) != _norm(AUTONOMY_COVERS)
            c["covers"] = AUTONOMY_COVERS
            c["scope"] = AUTONOMY_SCOPE
            c["never"] = AUTONOMY_NEVER
            if exception:
                c["exception"] = exception.replace('"', "'")
                c["never"] = 'the quote\'s own limit: "%s"; %s' % (c["exception"], AUTONOMY_NEVER)
                from . import lint as _lint  # lint imports this module, so the import waits until it is needed
                if _lint.forbidden_hits("- Never covers: " + c["never"]):
                    dropped.append((c, "the exception clause %r reads as a forbidden rule; add this grant by hand" % exception))
                    continue
            kept.append(c)
            continue
        covers = c.get("covers") or ""
        if not covers_named_by_quote(covers, c["quote"]):
            derived = derive_covers(c["quote"])
            if not derived:
                dropped.append((c, "Covers names actions the quote does not, and the quote names no class of action itself"))
                continue
            c["covers"] = derived
            c["covers_derived"] = True
        why = never_grantable(c["covers"])
        if why:
            dropped.append((c, why))
        elif spend_without_cap(c["covers"]):
            dropped.append((c, "a spend grant must state a cap"))
        elif covers_messages(c["covers"]) and not names_a_recipient(c["covers"], tokens):
            dropped.append((c, "covers messages without naming the person or channel"))
        else:
            c["names_recipient"] = names_a_recipient(c["covers"], tokens)
            kept.append(c)
    return kept, dropped


def _title(covers, cls="action"):
    if cls == "autonomy":
        return AUTONOMY_TITLE
    words = re.sub(r"[^A-Za-z0-9 ]+", " ", covers or "grant").split()
    t = " ".join(words[:8]).strip() or "Grant"
    return t[0].upper() + t[1:]


def render_grant(n, c):
    date = (c.get("time") or "")[:10] or "0000-00-00"
    quote = (c.get("quote") or "").replace("\n", " ").replace('"', "'").strip()
    cls = c.get("cls") or "action"
    lines = ["## G%d. %s" % (n, _title(c.get("covers"), cls)),
             '- Quote: "%s" (%s, %s, %s)' % (quote, date, c.get("origin") or "typed", c.get("session") or "unknown")]
    if c.get("source"):
        lines.append("- Source: %s" % c["source"])
    if cls == "autonomy":
        lines.append("- Class: autonomy")
    if cls == "action":
        # a grant whose Covers names its recipient covers exactly those messages, so the default does not deny them
        names = c.get("names_recipient") or names_a_recipient(c.get("covers"))
        default_never = ("anything the quote does not name; " + ("" if names else "messages or posts to other people; ")
                         + "permission, sandbox or security changes")
    else:
        default_never = AUTONOMY_NEVER
    lines += ["- Scope: %s" % (c.get("scope") or ("unspecified" if cls == "action" else AUTONOMY_SCOPE)),
              "- Covers: %s" % (c.get("covers") or "unspecified"),
              "- Never covers: %s" % (c.get("never") or default_never),
              "- Status: active", ""]
    return lines


def migrate_autonomy(text):
    """Rewrite the pipeline's own legacy autonomy Covers and Never covers lines to the current fixed text. Only lines
    that equal the legacy text exactly are touched, so a hand-narrowed line is left alone. Returns (text, changed)."""
    if not text:
        return text, 0
    out, changed = [], 0
    for line in text.split("\n"):
        if _norm(line) == _norm("- Covers: " + LEGACY_AUTONOMY_COVERS):
            line, changed = "- Covers: " + AUTONOMY_COVERS, changed + 1
        elif _norm(line).startswith(_norm("- Never covers:")) and _norm(line).endswith(_norm(LEGACY_AUTONOMY_NEVER)):
            line, changed = line[:len(line.rstrip()) - len(LEGACY_AUTONOMY_NEVER)] + AUTONOMY_NEVER, changed + 1
        out.append(line)
    return "\n".join(out), changed


def merge_grants(existing_text, candidates, name, tombstones=None):
    """Append candidates that are new to an existing grants.md (or build the file). A candidate is not new when its
    source record or its quote is already in the file, or is in `tombstones` (grants the user revoked or deleted:
    dicts with `source` and `quote`). Existing blocks are kept verbatim, so the user's edits survive.
    Returns (text, added, added_records) where added_records lists {id, source, quote} for each new block."""
    existing, _ = parse_grants(existing_text or "")
    have_q = {_norm(g.get("quote")) for g in existing if g.get("quote")}
    have_s = {g["source"] for g in existing if g.get("source")}
    for t in tombstones or []:
        if t.get("quote"):
            have_q.add(_norm(t["quote"]))
        if t.get("source"):
            have_s.add(t["source"])
    next_n = max([g["n"] for g in existing] + [0]) + 1
    out = existing_text.rstrip("\n") + "\n\n" if (existing_text or "").strip() else header(name) + "\n"
    added, records = 0, []
    for c in candidates:
        q = _norm(c.get("quote"))
        if q in have_q or (c.get("source") and c["source"] in have_s):
            continue
        out += "\n".join(render_grant(next_n, c)) + "\n"
        have_q.add(q)
        if c.get("source"):
            have_s.add(c["source"])
        records.append({"id": "G%d" % next_n, "source": c.get("source") or "", "quote": q})
        next_n += 1
        added += 1
    return out, added, records


_REVOKE_RE = re.compile(r"(?i)\b(?:never|don'?t|dont|do not|stop|no more|not any ?more|no longer|ask (?:me )?(?:first|before|again|every time)|"
                        r"always ask|check with me|revoke[ds]?|not allowed|forget (?:that|what i said)|scratch that|nvm|never ?mind)\b")


def _overlap(a, b):
    wa, wb = set(_stem(w) for w in _content_words(a)), set(_stem(w) for w in _content_words(b))
    if not wa:
        return 0.0
    return len(wa & wb) / float(len(wa))


def revoke_from_changes(text, changes, corpus_rows, today):
    """Apply mined preference_changes that revoke a grant: `earlier` matches a grant's quote (one contains the other,
    or three or more of its content words overlap at 60% or more) and `later` carries a revocation marker and exists
    verbatim in a typed corpus record. Those grants get `Status: revoked <date of the later message>`.
    Returns (text, [(grant id, later excerpt)])."""
    grants, _ = parse_grants(text or "")
    if not grants or not changes:
        return text, []
    typed = _typed_rows(corpus_rows)
    lines = (text or "").splitlines()
    revoked = []
    for ch in changes:
        later = ch.get("later") or ""
        earlier = ch.get("earlier") or ""
        if not later or not earlier or not _REVOKE_RE.search(later):
            continue
        ln = _norm(later)
        if not any(ln in t for _, t in typed):
            continue
        date = (ch.get("later_time") or "")[:10] or today
        for g in grants:
            if not g.get("active") or not g.get("quote"):
                continue
            qn, en = _norm(g["quote"]), _norm(earlier)
            if not (en in qn or qn in en or (len(_content_words(earlier)) >= 3 and _overlap(earlier, g["quote"]) >= 0.6)):
                continue
            a, b = g["lines"]
            for i in range(a - 1, b):
                if lines[i].startswith("- Status:"):
                    lines[i] = "- Status: revoked %s" % date
                    break
            g["active"] = False
            revoked.append((g["id"], later[:120]))
    if not revoked:
        return text, []
    return "\n".join(lines) + ("\n" if (text or "").endswith("\n") else ""), revoked


def tombstone(g, reason, date):
    return {"id": g.get("id", ""), "source": g.get("source") or "", "quote": _norm(g.get("quote")), "reason": reason, "date": date}


def merge_tombstones(old, new):
    """Union by (source, quote); the first entry for a key wins."""
    out, seen = [], set()
    for t in list(old or []) + list(new or []):
        key = (t.get("source") or "", t.get("quote") or "")
        if key == ("", "") or key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out
