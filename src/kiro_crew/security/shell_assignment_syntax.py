"""Shell assignment and operator syntax: the leaf readers the resolver is built on.

Control-operator and assignment spellings, the words that guard or scope an
assignment, the quote-state walk, and the glued-operator split.  Nothing here
tracks a binding: :mod:`.shell_normalizer` owns the resolver that does, and reads
these as its vocabulary.  Split out when that module reached the per-file line
cap of the package's monolith ratchet.
"""

from __future__ import annotations

import re

# The start of a redirection, with any descriptor prefix; for use where a redirect
# ENDS an argument list rather than hiding a program.  Testing only the first
# character missed every descriptor-prefixed spelling (``2>``, ``&>``, ``{fd}>``,
# ``1>``), which is exactly where a redirection is most often written -- so the
# descriptor read as an ordinary refspec and the command after the redirect was
# absorbed as arguments.
_REDIRECT_START_RE = re.compile(r"(?:\d+|&|\*|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:>{1,2}[&|!]?|<{1,3})")


# ``NAME=value``: a literal program name may reach its use only through the
# expansion, so neither the literal name nor the expansion alone looks dangerous.
# The assignment and the use are in the SAME command text, so the literal can be
# substituted back before any comparison.
_LOCAL_ASSIGN_RE = re.compile(r"\A([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z", re.DOTALL)


# `NAME=value` prefix: `normalize_shell_command` keeps it as a single token, and
# the value is already $HOME-expanded by the time it is read.
#: ``NAME=value`` and ``NAME+=value``. The append form is a separate group so a
#: caller can add to what it already recorded instead of replacing it. Matching
#: only ``=`` means the whole ``NAME+=`` token fails to match, so the segment
#: reads as a command word rather than an assignment.
_SHELL_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?)=(.*)$", re.DOTALL)


# A run of control operators: what separates one program from the next in a run
# that ``shlex`` handed over as a single word.
_CONTROL_OPERATOR_RE = re.compile(r"[;&|\n]+")
# The same split with the operators KEPT, so a script is walked segment by segment.
_CONTROL_OPERATOR_SPLIT_RE = re.compile(r"([;&|\n]+)")
# An assignment after ``||``/``&&`` may not run and one before ``|``/``&`` runs in a
# subshell: neither replaces the binding before it.  A run of assignments followed by
# a boundary (or nothing) persists; followed by a command word it is a prefix.
_CONDITIONAL_OPERATORS = frozenset({"||", "&&", "|", "|&"})
# The body of a compound command runs only when its test or pattern says so, so an
# assignment after one of these keywords (or after a ``case`` pattern, a word ending
# in ``)``) is guarded exactly as one after ``||``/``&&``: ``x=<cli>; if false; then
# x=echo; fi; $x <verb>`` runs the mint while the single reading took ``echo`` (found
# in review).  ``{`` is included: a group is a function body as often as a block.
_GUARD_WORDS = frozenset({"then", "do", "else", "elif", "{"})
# An assignment-shaped word after one of these is the shell's own binding; after any
# other command word it is that command's DATA (see ``_is_argument_assignment``).
_DECLARATION_BUILTINS = frozenset({"export", "declare", "typeset", "local", "readonly"})
_SUBSHELL_OPERATORS = frozenset({"|", "&", "|&"})
_COMMAND_BOUNDARY_TOKENS = frozenset({";", "||", "&&", "|", "|&", "&", ";;", ";&", ";;&"})
_SHELL_WORD_RE = re.compile(r"""(?:"[^"]*"|'[^']*'|\S)+""")


# The whitespace ``shlex`` splits on: one INSIDE a token is proof the token was quoted.
_SHLEX_WHITESPACE_RE = re.compile(r"[ \t\r\n]")


def _is_guard_token(token: str) -> bool:
    """True when an assignment right after *token* may not run (see ``_GUARD_WORDS``)."""
    return token in _CONDITIONAL_OPERATORS or token in _GUARD_WORDS or token.endswith(")")


def _guarded_body(segment: str) -> "tuple[bool, str]":
    """``(guarded, body)``: *segment* with a leading guard word (or the words up to a
    ``case`` pattern) removed when an assignment follows it, else the segment as is."""
    words = _SHELL_WORD_RE.findall(segment)
    for pos, word in enumerate(words[:-1]):
        if _is_guard_token(word) and _SHELL_ASSIGN_RE.match(words[pos + 1]):
            return True, " ".join(words[pos + 1 :])
    return False, segment


def _is_command_word(token: str) -> bool:
    """True when a prefix before *token* is scoped to it: not a boundary, redirection
    or comment.  Builtins are command words too: outside POSIX mode bash scopes a
    prefix to every builtin (``x=echo export y`` leaves ``x`` alone), and that is
    the reading that refuses; ``sh`` persisting it before a SPECIAL builtin only
    widens the refusal.  Regular builtins (``local``, ``declare``) never persist."""
    return not (
        token in _COMMAND_BOUNDARY_TOKENS
        or token.startswith("#")
        or _REDIRECT_START_RE.match(token) is not None
    )


def _leading_assignments(segment: str) -> "list[tuple[str, str, bool]]":
    """``(name, value, appends)`` for a segment's LEADING run of assignments.

    Both spellings open a command's prefix (``y=1 x=<cli>``, ``A+=foo x=<cli>``): an
    append in the run is still an assignment, so the command word comes after it.
    """
    pairs: list[tuple[str, str, bool]] = []
    for word in _SHELL_WORD_RE.findall(segment):
        assign = _SHELL_ASSIGN_RE.match(word)
        if not assign:
            break
        pairs.append((assign.group(1), assign.group(3).strip("\"'"), bool(assign.group(2))))
    return pairs


def _token_in_command_position(tokens: "list[str]", idx: int) -> bool:
    """True when ``tokens[idx]`` is the command word: what follows a boundary (or the
    start) and that command's leading assignments.  A boundary is a separator token
    or a token ENDING in one (``shlex`` glues ``true;``).  An APPEND is a leading
    assignment too (``A+=foo $x <verb>`` runs ``$x``): read with the plain spelling
    only, the append hid the command position and a multiword value stayed one word.
    """
    look = idx
    while look and _SHELL_ASSIGN_RE.match(tokens[look - 1]):
        look -= 1
    return look == 0 or _CONTROL_OPERATOR_RE.fullmatch(tokens[look - 1][-1:]) is not None


def _is_argument_assignment(tokens: "list[str]", idx: int) -> bool:
    """True when the assignment-shaped ``tokens[idx]`` is an ARGUMENT: it follows a
    command word other than a declaration builtin, so the shell hands it to that
    command as data (``echo x=echo``, ``make CFLAGS=-O2``) -- and the PIECES of a
    quoted operand (see :func:`_split_glued_operators`) land here too, because the
    operand's own separators put them after its command word.  Whether the command
    runs them (``eval``) is what the resolver reads both ways."""
    if _token_in_command_position(tokens, idx):
        return False
    look = idx
    while look and _SHELL_ASSIGN_RE.match(tokens[look - 1]):
        look -= 1
    return tokens[look - 1] not in _DECLARATION_BUILTINS


def _is_command_scoped_assignment(segment: str) -> bool:
    """True when a segment is ``NAME=value ... command`` (``X=foo true``), not a bare run."""
    rest = _SHELL_WORD_RE.findall(segment)[len(_leading_assignments(segment)) :]
    return bool(rest) and _is_command_word(rest[0])


def _quote_state_after(text: str, quote: str) -> str:
    """The quote (``'``, ``"`` or none) still OPEN after *text*, entered with *quote* open.

    A separator inside a quote is data, so a segment that opens inside one is not a
    command and cannot assign (``printf "%s" "; x=echo"``).  An unclosed quote stays
    open to the end: every later segment is then read as data, which only widens the
    outer binding's reach -- the refusal direction.
    """
    skip = False
    for ch in text:
        if skip:
            skip = False
        elif ch == "\\" and quote != "'":
            skip = True
        elif quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
    return quote


_TRAILING_OPERATOR_RE = re.compile(r"[;&|\n]+\Z")


def _trailing_operator(token: str) -> str:
    """The control-operator run *token* ends in (``|&`` of ``"$v token"|&``), or ``""``."""
    match = _TRAILING_OPERATOR_RE.search(token)
    return match.group(0) if match else ""


def _split_glued_operators(tokens: "list[str]") -> "list[str]":
    """Split tokens on control operators glued to their neighbours.

    ``shlex`` splits on whitespace only, so ``X=<name>;$X`` arrives as one token and an
    assignment glued to the command that uses it is invisible to both.  Splitting keeps
    the operator itself as a token so argv-boundary logic still sees it.

    A token that CONTAINS ``shlex`` whitespace was quoted, and a quoted word is ONE
    argument however many ``;`` it carries (``bash -c '<name>=<cli>; $<name> <verb>'``).
    Split into pieces ALONE, the payload walk -- which takes the ONE token after the
    carrier -- saw only ``<name>=<cli>``.  So such a token is yielded WHOLE first
    (the walk re-tokenizes it) and then its pieces, because the whitespace may sit
    inside a quoted VALUE of a top-level glued run (``X="a b";Y=<cli>;$Y <verb>``).
    """
    out: list[str] = []
    for token in tokens:
        # ONLY split a token that begins with an assignment.  Splitting any token
        # carrying a separator would destroy a QUOTED target -- ``shlex`` has already
        # removed the quotes, so ``pkill -f '[;]*<name>'`` arrives as the single token
        # ``[;]*<name>`` and is indistinguishable from a real separator at this point.
        # The reported evasion is specifically an assignment glued to its use, so that
        # is the only shape split here -- in both its spellings: ``q+=$p;`` left
        # glued kept the append out of the reading (found in review).
        if not (_LOCAL_ASSIGN_RE.match(token) or _SHELL_ASSIGN_RE.match(token)) or not (
            _CONTROL_OPERATOR_RE.search(token)
        ):
            out.append(token)
            continue
        quoted_whole = bool(_SHLEX_WHITESPACE_RE.search(token))
        if quoted_whole:
            out.append(token)  # quoted whole (see above): the carrier's operand first
        # The operator run is kept as spelled, so the resolver's guard sees ``||``.
        # In a quoted whole SCRIPT a separator INSIDE a quote it still carries is data
        # (``printf ";x=echo"`` is no assignment), so it stays glued to its word; an
        # UNCLOSED quote keeps the plain split, since ``shlex`` may have consumed the
        # escape that balanced it.  A token WITHOUT ``shlex`` whitespace was never a
        # script: ``shlex`` has already stripped its quoting level, so a quote it still
        # carries was ESCAPED (``q=\";x=<cli>;\";$x``) and is literal data, while the
        # separators beside it are real -- the plain split reads them.
        # A glued run is collected and joined ONCE when it ends: appending to a
        # list element re-copies the run per separator (quadratic on a long
        # quoted token).
        pieces: list[str] = []
        run: list[str] = []
        quote = ""
        balanced = quoted_whole and not _quote_state_after(token, "")
        for piece in _CONTROL_OPERATOR_SPLIT_RE.split(token):
            opens_quoted, quote = bool(quote), _quote_state_after(piece, quote)
            if balanced and opens_quoted and run:
                run.append(piece)
                continue
            if run:
                pieces.append("".join(run))
            run = [piece]
        if run:
            pieces.append("".join(run))
        for piece in pieces:
            if _CONTROL_OPERATOR_RE.fullmatch(piece):
                out.append(piece.strip() or ";")
            elif piece:
                out.append(piece)
    return out
