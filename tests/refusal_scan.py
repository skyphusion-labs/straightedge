"""Extract every refusal reason `risk.py` can NAME, from the AST (issue #61).

The roster in `test_refusal_reasons.py` is a DENOMINATOR: it asserts that every
reason literal in `risk.py` is named by a test, so the count cannot silently
drift. The first version scanned the module as TEXT with a regex that required a
closing quote, so it matched `reason="max_positions"` and could not see

    reason="spec_not_measured:" + ",".join(sorted(not_measured))

A reason the scanner cannot see is silently dropped, and the roster then reads
GREEN while `risk.py` carries a reason nothing covers. That is the failure this
repo keeps finding: an instrument that stops being able to measure its subject
and reports the reassuring answer instead of saying so.

Two things fix it, and only together:

1. Read the module as a TREE, not as text. A concatenation, an f-string and a
   module constant all resolve, because the shape of the expression stops
   mattering.

2. **Fail on a site that cannot be read.** This is the real fix, whatever the
   matching strategy: every site is classified, and anything the scanner cannot
   resolve lands in `unresolved`, which the caller asserts is empty. A scanner
   that skips a line it does not understand is back to the original defect in a
   more sophisticated costume.

`forwarded` is the other half of (2). Some sites genuinely do not name a reason,
they pass one along: `RiskDecision(reason=self._halt_reason)` reports a reason
named somewhere else in the same module, which this scanner reads at that other
site. Those cannot be treated as findings, and they must not become a silent
ignore list either, so they are RETURNED and the caller pins the exact set. A
new forwarding form has to be looked at by a person rather than quietly joining
the set of things not measured.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

#: The keyword argument every `RiskDecision` carries its reason in.
REASON_KEYWORD = "reason"
#: `self._halt("daily_loss")` names its reason positionally.
HALT_METHOD = "_halt"
#: `self._halt_reason = "state_unreadable"` names one by assignment.
HALT_ATTR = "_halt_reason"


@dataclass(frozen=True)
class ReasonScan:
    """What one pass over a module found.

    names        every reason the module can name, base name only
    prefixes     the subset whose literal is a PREFIX with a payload appended
                 at runtime (`spec_not_measured:point`), so a test knows to
                 assert `startswith` rather than equality
    forwarded    source expressions that pass a reason through from elsewhere.
                 Not findings; pinned by the caller so a NEW one is loud.
    unresolved   sites the scanner could not read at all. Never empty by
                 design: the caller asserts on it, because this is the list
                 that says the instrument has stopped measuring.
    """

    names: frozenset[str]
    prefixes: frozenset[str]
    forwarded: tuple[str, ...]
    unresolved: tuple[str, ...]


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"`, so `reason=SOME_CONSTANT` resolves.

    Without this a constant would look like a local variable and be filed as
    forwarding, which is how a named reason would go missing while every
    assertion here still passed.
    """
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                out[node.target.id] = value.value
    return out


def _is_self_method_call(node: ast.Call, name: str) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == name
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
    )


def _returns_str(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    ann = fn.returns
    return isinstance(ann, ast.Name) and ann.id == "str"


class _Collector:
    """Resolve one expression into literals, prefixes, forwards or unresolved."""

    def __init__(self, consts: dict[str, str]) -> None:
        self.consts = consts
        self.names: set[str] = set()
        self.prefixes: set[str] = set()
        self.forwarded: set[str] = set()
        self.unresolved: set[str] = set()

    def visit(self, expr: ast.expr, *, prefix: bool = False) -> None:
        # A plain literal: the whole reason, unless a caller upstream has told
        # us it is only the head of one.
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            self._record(expr.value, prefix=prefix)
            return
        # A module constant resolves to its literal; anything else named is a
        # local or a parameter, so the reason is named wherever it was set.
        if isinstance(expr, ast.Name):
            if expr.id in self.consts:
                self._record(self.consts[expr.id], prefix=prefix)
            else:
                self.forwarded.add(ast.unparse(expr))
            return
        if isinstance(expr, (ast.Attribute, ast.Call)):
            self.forwarded.add(ast.unparse(expr))
            return
        # `A + B`: the reason is whatever A is, with a payload appended. Only
        # the LEFT side can name it, so a concatenation whose left side cannot
        # be read is unresolved rather than ignored.
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            self.visit(expr.left, prefix=True)
            return
        # An f-string names a reason only if it opens with a literal.
        if isinstance(expr, ast.JoinedStr):
            head = expr.values[0] if expr.values else None
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                self._record(head.value, prefix=True)
            else:
                self._unresolved(expr)
            return
        # `x or "halted"` and `a if c else b` each name every branch.
        if isinstance(expr, ast.BoolOp):
            for value in expr.values:
                self.visit(value, prefix=prefix)
            return
        if isinstance(expr, ast.IfExp):
            self.visit(expr.body, prefix=prefix)
            self.visit(expr.orelse, prefix=prefix)
            return
        self._unresolved(expr)

    def _record(self, value: str, *, prefix: bool) -> None:
        base = value.rstrip(":") if prefix else value
        if not base:
            return  # `reason = ""` clears a reason, it does not name one
        self.names.add(base)
        if prefix:
            self.prefixes.add(base)

    def _unresolved(self, expr: ast.expr) -> None:
        line = getattr(expr, "lineno", 0)
        self.unresolved.add(f"line {line}: {ast.unparse(expr)}")


def scan_reasons(source: str, *, class_name: str = "RiskManager") -> ReasonScan:
    """Every refusal reason `source` can name, and every site it could not read.

    Sites examined:
      - any `reason=` keyword argument, at any call
      - `self._halt(<reason>)`, which names its reason positionally
      - any assignment to `self._halt_reason`
      - every `return` in a `-> str` method of `class_name`, which is how
        `circuit_reason` and `clear_operator_halt` report one
    """
    tree = ast.parse(source)
    collector = _Collector(_module_string_constants(tree))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == REASON_KEYWORD:
                    collector.visit(kw.value)
            if _is_self_method_call(node, HALT_METHOD) and node.args:
                collector.visit(node.args[0])
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == HALT_ATTR:
                    collector.visit(node.value)
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if (
                isinstance(target, ast.Attribute)
                and target.attr == HALT_ATTR
                and node.value is not None
            ):
                collector.visit(node.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for fn in ast.walk(node):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not _returns_str(fn):
                    continue
                for ret in ast.walk(fn):
                    if isinstance(ret, ast.Return) and ret.value is not None:
                        collector.visit(ret.value)

    return ReasonScan(
        names=frozenset(collector.names),
        prefixes=frozenset(collector.prefixes),
        forwarded=tuple(sorted(collector.forwarded)),
        unresolved=tuple(sorted(collector.unresolved)),
    )

#: Functions that are the AUTHORITY for one refusal word, returning the reason
#: a value earns or `None`. `sizing.unusable_stop` and `sizing.unusable_volume`
#: are consulted by `engine.py` and rendered to the operator as
#: `refused: <reason>`, so their words are part of the operator vocabulary even
#: though no `RiskDecision` ever carries them.
REASON_AUTHORITIES = ("unusable_stop", "unusable_volume")

#: A reason WORD is a name this project defines: lower snake_case, optionally
#: ending in `:` when a payload is appended at runtime. Anything else after
#: `refused: ` is operator prose.
REASON_WORD = re.compile(r"^[a-z][a-z0-9_]*:?$")

#: EVERY KIND of non-word site `scan_refusal_literals` can put in `forwarded`,
#: declared here so a gate can assert it covers all of them instead of listing
#: prefixes from memory.
#:
#: This exists because of a measured defect: the scanner gained the `composed: `
#: kind and the gate that pins non-word sites kept filtering on the two kinds it
#: knew, so a concatenated refusal was surfaced by the instrument and ignored by
#: the gate. Nothing in the types said so and a green run could not tell.
#: Asserting that a bucket is EMPTY would not have caught it either, since
#: `forwarded` legitimately holds eight entries here; the claim that holds is
#: that the gate's filter covers every kind this scanner can emit.
NON_WORD_KIND_PROSE = "prose: "
NON_WORD_KIND_INTERPOLATED = "interpolated: "
NON_WORD_KIND_COMPOSED = "composed: "
NON_WORD_KINDS = (
    NON_WORD_KIND_COMPOSED,
    NON_WORD_KIND_INTERPOLATED,
    NON_WORD_KIND_PROSE,
)


def scan_reason_authorities(
    source: str, *, function_names: tuple[str, ...] = REASON_AUTHORITIES
) -> ReasonScan:
    """Every word the named module-level functions can return.

    Needed because both scanners above are keyed on `RiskDecision`, and a
    refusal word reaching the operator does not have to come through one.
    `unusable_stop` returns `sl_required` and `sl_not_measured:<value>`;
    `unusable_volume` returns `volume_unusable:<value>`. Reading `risk.py` and
    `engine.py` alone therefore UNDERCOUNTS the vocabulary, which is the same
    shape as the drift `scan_decision_reasons` exists for: the population
    moved, not the scanner.

    Reuses `_Collector`, so an f-string, a concatenation and a module constant
    all resolve, and a return it cannot read lands in `unresolved` rather than
    being skipped. `return None` names no reason and is skipped deliberately.
    """
    tree = ast.parse(source)
    collector = _Collector(_module_string_constants(tree))
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in function_names:
            continue
        seen.add(node.name)
        for ret in ast.walk(node):
            if isinstance(ret, ast.Return) and ret.value is not None:
                if isinstance(ret.value, ast.Constant) and ret.value.value is None:
                    continue
                collector.visit(ret.value)
    # A NAME THAT IS NOT THERE IS NOT A CLEAN READ. Without this, renaming
    # `unusable_volume` would shrink the denominator silently and every caller
    # would still pass, which is this module's own documented failure mode.
    for wanted in function_names:
        if wanted not in seen:
            collector.unresolved.add(f"function not found: {wanted}")
    return ReasonScan(
        names=frozenset(collector.names),
        prefixes=frozenset(collector.prefixes),
        forwarded=tuple(sorted(collector.forwarded)),
        unresolved=tuple(sorted(collector.unresolved)),
    )


def scan_refusal_literals(source: str) -> ReasonScan:
    """Reason words written directly after `refused: ` in a reply string.

    `desk.py` answers `refused: halted` and `refused: max_advice_turns_per_day`
    as plain literals, with no decision object involved, so these are invisible
    to every scanner above. An interpolated site (`f"refused: {reason}"`) names
    nothing here and is correctly skipped: the word is named wherever that
    expression was set, and the scanners above read it there.

    NOT EVERY REFUSAL REPLY IS A REASON WORD, and `forwarded` carries two
    kinds of non-word site so that neither is dropped:

    * `prose: ...` for a literal with no word shape, such as
      `refused: unresolved send <client_id>`, which names one in-flight send.
    * `interpolated: ...` for an f-string whose head is exactly `refused: `,
      so nothing literal follows it. Either the word is named elsewhere
      (`f"refused: {decision.reason}"`) or the payload is free text nobody
      defines (`f"refused: {result.comment}"`). The scanner cannot tell those
      apart by dataflow and does not try; it returns the expression and the
      caller pins the set.

    Both are RETURNED rather than counted or ignored. Counting them would
    corrupt the denominator with text that can never be documented as a word;
    dropping them is the defect this module was written against, and TWO
    earlier versions of this function did exactly that: the first to every
    f-string site, the second to every CONCATENATED one, which is how most
    people would spell it.

    WHAT IS SEEN, AND THE ONE THING THAT IS NOT. The rule is not a list of
    blessed shapes: a refusal reply has to carry the literal prefix somewhere
    in the source, so every string constant carrying it is a site whatever
    expression assembles the rest. Measured per form in
    `tests/test_contract_refusal_vocabulary.py`: f-string, concatenation with
    and without the space, `str.join`, an f-string with a LEADING expression,
    `%` and `.format` are all seen. A docstring mentioning the prefix is not a
    site and is skipped.

    **The residual blind spot is a COMPUTED prefix** (`"ref" + "used: "`),
    which leaves no literal to find and which no scan of this kind can see.
    That limit is pinned by a test, so closing it later reds and forces this
    paragraph to be updated rather than letting the claim drift.
    """
    tree = ast.parse(source)
    collector = _Collector(_module_string_constants(tree))

    # THE INVARIANT, stated because the previous two versions of this function
    # each claimed closure they did not have. A refusal reply has to contain
    # the literal prefix SOMEWHERE in the source, so every string Constant
    # carrying it is a site, whatever expression assembles the rest. That is
    # spelling-agnostic by construction: f-string, concatenation, `join`, `%`
    # and `.format` all reach here, because all of them leave the prefix as a
    # literal. The one residual blind spot is a COMPUTED prefix
    # (`"ref" + "used: "`), which no literal scan can see and which nobody
    # writes.
    #
    # The earlier versions enumerated shapes instead. The first returned on an
    # f-string head and lost that whole kind; the second fixed the f-string
    # and left `"refused: " + x.comment` invisible, which is how most people
    # would spell it, while the comment claimed the hole was closed.
    docstrings = set()
    for holder in ast.walk(tree):
        if isinstance(holder, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(holder, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    fstring_heads = set()
    for holder in ast.walk(tree):
        if isinstance(holder, ast.JoinedStr) and holder.values:
            head_node = holder.values[0]
            if isinstance(head_node, ast.Constant) and isinstance(head_node.value, str):
                fstring_heads.add(id(head_node))

    parent: dict[int, ast.AST] = {}
    for holder in ast.walk(tree):
        for child in ast.iter_child_nodes(holder):
            parent[id(child)] = holder

    def enclosing_statement(node: ast.AST) -> str:
        cur: ast.AST | None = node
        while cur is not None and not isinstance(cur, ast.stmt):
            cur = parent.get(id(cur))
        if cur is None:
            return ast.unparse(node)
        text = " ".join(ast.unparse(cur).split())
        return text if len(text) <= 90 else text[:87] + "..."

    def take(text: str) -> None:
        if not text.startswith("refused: "):
            return
        tail = text[len("refused: ") :]
        if not tail:
            return  # handled by the caller, which knows how it is composed
        if REASON_WORD.match(tail):
            collector._record(tail, prefix=True)
        else:
            collector.forwarded.add(NON_WORD_KIND_PROSE + text.rstrip())

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings or id(node) in fstring_heads:
                continue  # prose about refusals, or handled below as the f-string
            if node.value in ("refused: ", "refused:"):
                # A BARE PREFIX WITH NOTHING LITERAL AFTER IT, and not an
                # f-string head, so the rest is composed some other way:
                # `"refused: " + x.comment`, a `join`, a `%`. The site is real
                # and names no word here, so it is surfaced with the statement
                # that builds it rather than dropped.
                collector.forwarded.add(NON_WORD_KIND_COMPOSED + enclosing_statement(node))
                continue
            take(node.value)
        elif isinstance(node, ast.JoinedStr):
            head = node.values[0] if node.values else None
            if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
                continue
            if head.value == "refused: ":
                # NOTHING LITERAL FOLLOWS `refused: `, so this site names no
                # word HERE. An earlier version returned early on exactly this
                # string and the site vanished: not a name, not a prefix, not
                # forwarded. Demonstrated by injecting a NEW
                # `f"refused: {x.comment}"` site and getting byte-identical
                # scanner output, which made the hole sit precisely in the
                # mechanism meant to force a person to look at a new refusal.
                #
                # Two kinds reach here and the scanner cannot tell them apart
                # by dataflow, which is exactly why it must not try: either the
                # word is named elsewhere and another scanner reads it there
                # (`f"refused: {decision.reason}"`), or the payload is free
                # text nobody defines (`f"refused: {result.comment}"`, whose
                # value comes from `OrderResult.not_sent`). So the EXPRESSION
                # is returned and the caller pins the set, the same contract
                # `forwarded` already carries for `RiskDecision(reason=...)`.
                # A new site of either kind then fails until a person says
                # which it is.
                collector.forwarded.add(NON_WORD_KIND_INTERPOLATED + ast.unparse(node))
                continue
            take(head.value)

    return ReasonScan(
        names=frozenset(collector.names),
        prefixes=frozenset(collector.prefixes),
        forwarded=tuple(sorted(collector.forwarded)),
        unresolved=tuple(sorted(collector.unresolved)),
    )


DECISION_CLASS = "RiskDecision"


def scan_decision_reasons(source: str) -> ReasonScan:
    """Every reason a `RiskDecision(...)` in `source` can carry.

    `scan_reasons` is deliberately broad because a RiskManager refusal comes out
    several ways (a `reason=` kwarg, `self._halt`, `self._halt_reason`, a bare
    return from a `-> str` method). That breadth is wrong for any OTHER module:
    `engine.py` has more than a dozen `reason=` kwargs that are journal fields,
    not refusals ("fill", "manual", "reverse", "closeby"), and every `-> str`
    method of `Engine` returns operator prose. Scanning it with `scan_reasons`
    would not widen the denominator, it would corrupt it.

    So this pass is NARROW on purpose: only `reason=` on a literal
    `RiskDecision(...)` construction. That is exactly the population the roster
    is a denominator for, and it finds it wherever the construction lives rather
    than only in the file the roster happened to be written against.

    Needed because `engine.py` began building `RiskDecision` directly: on `main`
    it built zero, so reading `risk.py` alone WAS complete, and it silently
    stopped being complete when the population moved rather than when the
    scanner changed.
    """
    tree = ast.parse(source)
    collector = _Collector(_module_string_constants(tree))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        named = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        if named != DECISION_CLASS:
            continue
        for kw in node.keywords:
            if kw.arg == REASON_KEYWORD:
                collector.visit(kw.value)
    return ReasonScan(
        names=frozenset(collector.names),
        prefixes=frozenset(collector.prefixes),
        forwarded=tuple(sorted(collector.forwarded)),
        unresolved=tuple(sorted(collector.unresolved)),
    )


#: The `OrderResult` factory that carries OUR OWN refusal word on the modify
#: path. `OrderResult.invalid_stops(<comment>)` is rendered to the operator as
#: `<verb> failed retcode=<n> <comment>`, so whatever is passed to it is
#: operator-visible text and not an internal field.
STOPS_FACTORY = "invalid_stops"


def scan_method_reasons(
    source: str, *, class_name: str, method_names: tuple[str, ...]
) -> ReasonScan:
    """Every word the named METHODS of `class_name` can return.

    Needed because the scanners above are keyed on `RiskDecision` or on the
    literal `refused: ` prefix, and `_stop_guard` is neither: it returns a bare
    word that `_modify` hands to `OrderResult.invalid_stops`, which the desk
    renders as `sl failed retcode=<n> <word>`. A word on that channel is as
    operator-visible as one after `refused: ` and was invisible to every
    scanner in this module (#228).

    `return ""` is the guard's PASS and names no reason, so `_Collector`
    dropping the empty string is the behaviour this relies on. A return it
    cannot read lands in `unresolved` rather than being skipped, and a method
    that is not found lands there too, because a renamed method would otherwise
    shrink the denominator in silence while every caller stayed green.
    """
    tree = ast.parse(source)
    collector = _Collector(_module_string_constants(tree))
    seen: set[str] = set()
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef) or cls.name != class_name:
            continue
        for node in ast.walk(cls):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in method_names:
                continue
            seen.add(node.name)
            for ret in ast.walk(node):
                if isinstance(ret, ast.Return) and ret.value is not None:
                    if isinstance(ret.value, ast.Constant) and ret.value.value is None:
                        continue
                    collector.visit(ret.value)
    for wanted in method_names:
        if wanted not in seen:
            collector.unresolved.add(f"method not found: {class_name}.{wanted}")
    return ReasonScan(
        names=frozenset(collector.names),
        prefixes=frozenset(collector.prefixes),
        forwarded=tuple(sorted(collector.forwarded)),
        unresolved=tuple(sorted(collector.unresolved)),
    )


def scan_factory_comments(source: str, *, factory: str = STOPS_FACTORY) -> ReasonScan:
    """Every comment passed to `OrderResult.<factory>(...)` in `source`.

    THE POPULATION, where `scan_method_reasons` gives the VOCABULARY. The two
    are different questions and the gap between them is the point: a comment
    here that is not a word the guard returns is operator PROSE, and the caller
    reconciles the difference rather than pattern-matching the text. That is
    why this returns everything it can read instead of deciding what counts.

    `invalid_stops(reason)` names nothing here and lands in `forwarded`, which
    is correct and load-bearing: it is the site where the guard's words enter
    this channel, so a caller can assert the wiring still exists rather than
    assuming it.

    A factory call with no positional argument lands in `unresolved`; so does
    a factory name that appears nowhere, for the same reason as above.
    """
    tree = ast.parse(source)
    collector = _Collector(_module_string_constants(tree))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != factory:
            continue
        found = True
        if node.args:
            collector.visit(node.args[0])
        else:
            line = getattr(node, "lineno", 0)
            collector.unresolved.add(f"line {line}: {factory} with no comment")
    if not found:
        collector.unresolved.add(f"factory not found: {factory}")
    return ReasonScan(
        names=frozenset(collector.names),
        prefixes=frozenset(collector.prefixes),
        forwarded=tuple(sorted(collector.forwarded)),
        unresolved=tuple(sorted(collector.unresolved)),
    )
