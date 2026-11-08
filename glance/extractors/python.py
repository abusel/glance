"""Python extraction via the stdlib ast module.

Understands FastAPI routes, Pydantic / dataclass / settings models, SQLAlchemy and
SQLModel tables, Alembic revisions, Celery tasks, Pydantic AI agents, prompt and
config constants, env var reads, and every function / method signature.
"""

from __future__ import annotations

import ast
import re

from ..model import Entity, short_hash

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace", "websocket", "api_route"}
PYDANTIC_BASES = {"BaseModel", "BaseSettings", "RootModel", "GenericModel", "SQLModel", "Schema"}
TABLE_BASES = {"Base", "DeclarativeBase", "Model", "db.Model", "AsyncAttrs"}
COLUMN_CALLS = {"Column", "mapped_column", "relationship", "column_property", "Field"}
CONST_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,}$")
PROMPT_RE = re.compile(r"PROMPT|INSTRUCTION|SYSTEM_MESSAGE|TEMPLATE", re.I)
VALIDATOR_DECOS = {"validator", "field_validator", "model_validator", "root_validator", "computed_field"}


def dotted(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return dotted(node.func)
    if isinstance(node, ast.Subscript):
        return dotted(node.value)
    return ""


def compact(src: str, limit: int = 220) -> str:
    s = re.sub(r"\s+", " ", src).strip()
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover
        return ""


def const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return unparse(node)
    return None


def is_literalish(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(is_literalish(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is None or is_literalish(k) for k in node.keys) and all(is_literalish(v) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        return is_literalish(node.operand)
    if isinstance(node, ast.BinOp):
        return is_literalish(node.left) and is_literalish(node.right)
    if isinstance(node, ast.Call):
        return dotted(node.func).split(".")[-1] in {"Decimal", "timedelta", "frozenset", "set", "tuple"} and all(
            is_literalish(a) for a in node.args
        ) and all(is_literalish(k.value) for k in node.keywords)
    if isinstance(node, ast.Attribute):  # Enum members like Plan.PRO
        return True
    return False


def body_hash(stmts: list[ast.stmt]) -> str:
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(getattr(stmts[0], "value", None), ast.Constant) and isinstance(stmts[0].value.value, str):
        stmts = stmts[1:]  # ignore docstring-only edits
    return short_hash("\n".join(unparse(s) for s in stmts))


def start_line(node: ast.AST) -> int:
    decos = getattr(node, "decorator_list", None) or []
    return min([node.lineno] + [d.lineno for d in decos])


class _Extractor:
    def __init__(self, path: str, text: str):
        self.path = path
        self.text = text
        self.tree = ast.parse(text)
        self.entities: list[Entity] = []
        self.router_prefix: dict[str, str] = {}
        self.local_pydantic: set[str] = set()
        self.local_tables: set[str] = set()
        self.agent_vars: dict[str, Entity] = {}

    def ent(self, kind: str, name: str, node: ast.AST, **kw) -> Entity:
        e = Entity(kind=kind, name=name, file=self.path, line=start_line(node),
                   end_line=getattr(node, "end_lineno", None) or node.lineno, lang="python", **kw)
        self.entities.append(e)
        return e

    def seg(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.text, node) or unparse(node)

    # ------------------------------------------------------------------ run
    def run(self) -> list[Entity]:
        mod = self.tree
        self._scan_module_assigns(mod)
        self._classify_classes(mod)
        is_alembic = self._alembic(mod)
        for node in mod.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not (is_alembic and node.name in {"upgrade", "downgrade"}):
                    self._function(node, None)
            elif isinstance(node, ast.ClassDef):
                self._class(node)
        self._env_vars(mod)
        return self.entities

    # ------------------------------------------------------------------ module level
    def _scan_module_assigns(self, mod: ast.Module) -> None:
        for node in mod.body:
            targets: list[ast.expr] = []
            value = None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            if len(targets) != 1 or not isinstance(targets[0], ast.Name) or value is None:
                continue
            name = targets[0].id
            if isinstance(value, ast.Call):
                fn = dotted(value.func).split(".")[-1]
                if fn in {"APIRouter", "FastAPI"}:
                    prefix = next((const_str(k.value) for k in value.keywords if k.arg == "prefix"), "") or ""
                    self.router_prefix[name] = prefix
                    continue
                if fn == "Agent":
                    self._agent(name, node, value)
                    continue
            if name in {"revision", "down_revision", "branch_labels", "depends_on"}:
                continue
            if CONST_RE.match(name) and is_literalish(value):
                sval = const_str(value)
                if sval is not None and (PROMPT_RE.search(name) or len(sval) > 280):
                    self.ent("agent", name, node, framework="prompt", fields={"text": sval},
                             body_hash=short_hash(sval), signature=compact(sval, 90))
                else:
                    self.ent("config", name, node, framework="constant", signature=compact(self.seg(value), 160),
                             body_hash=short_hash(unparse(value)))
            elif name.isupper() and isinstance(value, ast.Dict) and name.endswith(("SCHEDULE", "CONFIG", "SETTINGS", "LIMITS", "RATES")):
                self.ent("config", name, node, framework="constant", signature=compact(self.seg(value), 160),
                         body_hash=short_hash(unparse(value)))

    def _agent(self, name: str, node: ast.stmt, call: ast.Call) -> None:
        fields: dict[str, str] = {}
        if call.args:
            fields["model"] = compact(self.seg(call.args[0]), 80)
        for kw in call.keywords:
            if kw.arg:
                fields[kw.arg] = compact(self.seg(kw.value), 400 if "prompt" in kw.arg or "instructions" in kw.arg else 120)
        e = self.ent("agent", name, node, framework="pydantic-ai", fields=fields, signature=f"Agent({fields.get('model', '')})")
        e.meta["tools"] = []
        self.agent_vars[name] = e

    # ------------------------------------------------------------------ classes
    def _base_names(self, cls: ast.ClassDef) -> list[str]:
        return [dotted(b) for b in cls.bases]

    def _classify_classes(self, mod: ast.Module) -> None:
        # Two passes so `class UserCreate(UserBase)` resolves to a model when UserBase is local.
        for _ in range(3):
            for node in ast.walk(mod):
                if not isinstance(node, ast.ClassDef):
                    continue
                bases = {b.split(".")[-1] for b in self._base_names(node)} | set(self._base_names(node))
                table_kw = any(k.arg == "table" and isinstance(k.value, ast.Constant) and k.value.value for k in node.keywords)
                has_tablename = any(
                    isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in s.targets)
                    for s in node.body
                )
                has_columns = any(self._column_call(s) for s in node.body)
                if table_kw or has_tablename or (bases & (TABLE_BASES | self.local_tables) and has_columns):
                    self.local_tables.add(node.name)
                elif bases & (PYDANTIC_BASES | self.local_pydantic):
                    self.local_pydantic.add(node.name)

    def _column_call(self, stmt: ast.stmt) -> bool:
        value = getattr(stmt, "value", None)
        return isinstance(value, ast.Call) and dotted(value.func).split(".")[-1] in {"Column", "mapped_column", "relationship"}

    def _class(self, cls: ast.ClassDef) -> None:
        bases = self._base_names(cls)
        short = {b.split(".")[-1] for b in bases}
        decos = {dotted(d).split(".")[-1] for d in cls.decorator_list}
        kind = framework = None
        if cls.name in self.local_tables:
            kind = "table"
            framework = "sqlmodel" if "SQLModel" in short else "sqlalchemy"
        elif cls.name in self.local_pydantic:
            kind, framework = "model", ("settings" if short & {"BaseSettings"} else "pydantic")
        elif "dataclass" in decos or "dataclass" in {dotted(d) for d in cls.decorator_list}:
            kind, framework = "model", "dataclass"
        elif "TypedDict" in short:
            kind, framework = "model", "typeddict"
        elif short & {"Enum", "StrEnum", "IntEnum"}:
            kind, framework = "type", "enum"

        if kind:
            fields: dict[str, str] = {}
            validators: dict[str, str] = {}
            meta: dict = {"bases": bases}
            for s in cls.body:
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
                    desc = unparse(s.annotation)
                    if s.value is not None:
                        desc += " = " + unparse(s.value)
                    fields[s.target.id] = compact(desc, 200)
                elif isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
                    tname = s.targets[0].id
                    if tname == "__tablename__":
                        meta["tablename"] = const_str(s.value) or unparse(s.value)
                    elif tname == "__table_args__":
                        meta["table_args"] = compact(unparse(s.value), 200)
                    elif tname == "model_config" or tname == "Config":
                        meta["config"] = compact(unparse(s.value), 200)
                    elif kind == "type":
                        fields[tname] = compact(unparse(s.value), 120)
                    elif isinstance(s.value, ast.Call) and dotted(s.value.func).split(".")[-1] in COLUMN_CALLS:
                        fields[tname] = compact(unparse(s.value), 200)
                elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    dn = {dotted(d).split(".")[-1] for d in s.decorator_list}
                    if dn & VALIDATOR_DECOS:
                        validators[s.name] = body_hash(s.body)
                elif isinstance(s, ast.ClassDef) and s.name == "Config":
                    meta["config"] = compact(unparse(s), 200)
            if validators:
                meta["validators"] = validators
            name = cls.name
            e = self.ent(kind, name, cls, framework=framework, fields=fields, meta=meta,
                         signature=f"class {cls.name}({', '.join(bases)})")
            if kind == "table" and meta.get("tablename"):
                e.signature += f"  →  table \"{meta['tablename']}\""
            if framework == "settings":
                prefix = ""
                cfg = meta.get("config", "")
                m = re.search(r"env_prefix\W+['\"]([^'\"]*)", cfg)
                if m:
                    prefix = m.group(1)
                for fname in fields:
                    self.ent("env", (prefix + fname).upper(), cls, framework="settings",
                             fields={"default": fields[fname]}, meta={"source": f"{cls.name}.{fname}"})

        for s in cls.body:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function(s, cls.name)

    # ------------------------------------------------------------------ functions
    def _signature(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, method: bool) -> str:
        args = fn.args
        a = ast.arguments(
            posonlyargs=args.posonlyargs, args=list(args.args), vararg=args.vararg,
            kwonlyargs=args.kwonlyargs, kw_defaults=args.kw_defaults, kwarg=args.kwarg, defaults=args.defaults,
        )
        if method and a.args and a.args[0].arg in {"self", "cls"}:
            a.args = a.args[1:]
            if len(a.defaults) > len(a.args):
                a.defaults = a.defaults[-len(a.args):] if a.args else []
        sig = f"{'async ' if isinstance(fn, ast.AsyncFunctionDef) else ''}def {fn.name}({unparse(a)})"
        if fn.returns is not None:
            sig += f" -> {unparse(fn.returns)}"
        return compact(sig, 400)

    def _params(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str]:
        out: dict[str, str] = {}
        args = fn.args.args
        defaults = [None] * (len(args) - len(fn.args.defaults)) + list(fn.args.defaults)
        for arg, d in list(zip(args, defaults)) + list(zip(fn.args.kwonlyargs, fn.args.kw_defaults)):
            if arg.arg in {"self", "cls"}:
                continue
            desc = unparse(arg.annotation) if arg.annotation is not None else "Any"
            if d is not None:
                desc += " = " + unparse(d)
            out[arg.arg] = compact(desc, 160)
        return out

    def _function(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, owner: str | None) -> None:
        qual = f"{owner}.{fn.name}" if owner else fn.name
        sig = self._signature(fn, owner is not None)
        lines = (fn.end_lineno or fn.lineno) - fn.lineno + 1
        self.ent("function", qual, fn, framework="", signature=sig, body_hash=body_hash(fn.body),
                 meta={"lines": lines, "async": isinstance(fn, ast.AsyncFunctionDef)})

        for d in fn.decorator_list:
            dn = dotted(d)
            parts = dn.split(".")
            last = parts[-1] if parts else ""
            # FastAPI routes
            if isinstance(d, ast.Call) and last in HTTP_METHODS and len(parts) >= 2:
                router = parts[-2]
                path = const_str(d.args[0]) if d.args else None
                path = path if path is not None else next((const_str(k.value) for k in d.keywords if k.arg == "path"), "")
                methods = [last.upper()]
                if last == "api_route":
                    mk = next((k.value for k in d.keywords if k.arg == "methods"), None)
                    methods = [const_str(e) or "?" for e in getattr(mk, "elts", [])] or ["ANY"]
                full = (self.router_prefix.get(router, "") + (path or "")) or "/"
                deps = []
                for k in d.keywords:
                    if k.arg == "dependencies":
                        deps += [compact(unparse(e), 80) for e in getattr(k.value, "elts", [])]
                params = self._params(fn)
                for pname, pdesc in params.items():
                    if "Depends(" in pdesc or "Security(" in pdesc:
                        deps.append(pdesc.split("=", 1)[-1].strip())
                    elif re.search(r"Annotated\[.*Depends\(", pdesc):
                        deps.append(pdesc)
                meta = {
                    "methods": methods,
                    "path": full,
                    "handler": qual,
                    "dependencies": deps,
                    "response_model": next((unparse(k.value) for k in d.keywords if k.arg == "response_model"), unparse(fn.returns) if fn.returns is not None else ""),
                    "status_code": next((unparse(k.value) for k in d.keywords if k.arg == "status_code"), ""),
                }
                self.ent("route", f"{'|'.join(methods)} {full}", fn, framework="fastapi",
                         signature=f"{'|'.join(methods)} {full} → {qual}()", fields=params, meta=meta)
            # Celery tasks
            elif last in {"task", "shared_task", "periodic_task"} and (last != "task" or len(parts) >= 2):
                opts = {}
                if isinstance(d, ast.Call):
                    opts = {k.arg: compact(unparse(k.value), 80) for k in d.keywords if k.arg}
                self.ent("task", qual, fn, framework="celery", signature=sig, fields=self._params(fn), meta={"options": opts})
            # Pydantic AI tools / prompt functions on a known agent
            elif len(parts) == 2 and parts[0] in self.agent_vars and parts[1] in {"tool", "tool_plain", "system_prompt", "instructions", "output_validator", "result_validator"}:
                self.agent_vars[parts[0]].meta["tools"].append(f"{parts[1]}: {qual}")

    # ------------------------------------------------------------------ alembic
    def _alembic(self, mod: ast.Module) -> bool:
        revision = down = None
        funcs: dict[str, ast.FunctionDef] = {}
        for node in mod.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
                if isinstance(tgt, ast.Name) and tgt.id == "revision":
                    revision = const_str(node.value)
                elif isinstance(tgt, ast.Name) and tgt.id == "down_revision":
                    down = unparse(node.value)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"upgrade", "downgrade"}:
                funcs[node.name] = node
        if not revision or "upgrade" not in funcs:
            return False
        doc = ast.get_docstring(mod) or ""
        message = doc.strip().splitlines()[0] if doc.strip() else ""
        up = self._alembic_ops(funcs["upgrade"])
        down_ops = self._alembic_ops(funcs["downgrade"]) if "downgrade" in funcs else []
        down_fn = funcs.get("downgrade")
        empty_down = down_fn is None or all(
            isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in down_fn.body
        )
        raises = down_fn is not None and any(isinstance(s, ast.Raise) for s in ast.walk(down_fn))
        self.ent("migration", revision, mod.body[0] if mod.body else mod, framework="alembic",
                 signature=message or revision,
                 meta={"revision": revision, "down_revision": down, "message": message,
                       "upgrade": up, "downgrade": down_ops, "downgrade_empty": empty_down or raises})
        self.entities[-1].line = 1
        self.entities[-1].end_line = len(self.text.splitlines())
        return True

    def _alembic_ops(self, fn: ast.FunctionDef) -> list[dict]:
        ops: list[dict] = []
        batch_tables: dict[str, str] = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.With):
                for item in node.items:
                    c = item.context_expr
                    if isinstance(c, ast.Call) and dotted(c.func).endswith("batch_alter_table") and isinstance(item.optional_vars, ast.Name):
                        batch_tables[item.optional_vars.id] = const_str(c.args[0]) if c.args else "?"
            if not isinstance(node, ast.Call):
                continue
            dn = dotted(node.func)
            parts = dn.split(".")
            if len(parts) != 2 or (parts[0] != "op" and parts[0] not in batch_tables):
                continue
            opname = parts[1]
            if opname == "batch_alter_table":
                continue
            args = list(node.args)
            table = batch_tables.get(parts[0])
            if table is None and args:
                table = const_str(args[0]) or unparse(args[0])
                args = args[1:]
            column = None
            detail = ""
            if opname == "add_column" and args and isinstance(args[0], ast.Call):
                col = args[0]
                column = const_str(col.args[0]) if col.args else None
                detail = compact(unparse(col), 200)
            elif opname in {"drop_column", "alter_column"} and args:
                column = const_str(args[0])
                detail = compact(", ".join(unparse(k) for k in node.keywords), 200)
            elif opname == "create_table":
                cols = []
                for a in args:
                    if isinstance(a, ast.Call) and dotted(a.func).split(".")[-1] == "Column" and a.args:
                        cols.append(compact(unparse(a), 140))
                detail = "; ".join(cols)
            elif opname == "execute":
                detail = compact(const_str(node.args[0]) or unparse(node.args[0]) if node.args else "", 300)
                table = None
            else:
                detail = compact(", ".join([unparse(a) for a in args] + [unparse(k) for k in node.keywords]), 200)
            ops.append({"op": opname, "table": table, "column": column, "detail": detail, "line": node.lineno})
        ops.sort(key=lambda o: o["line"])
        return ops

    # ------------------------------------------------------------------ env
    def _env_vars(self, mod: ast.Module) -> None:
        seen: set[str] = set()
        for node in ast.walk(mod):
            name = default = None
            if isinstance(node, ast.Subscript) and dotted(node.value) in {"os.environ", "environ"}:
                name = const_str(node.slice)
            elif isinstance(node, ast.Call):
                fn = dotted(node.func)
                if fn in {"os.getenv", "getenv", "os.environ.get", "environ.get", "os.environ.setdefault"} and node.args:
                    name = const_str(node.args[0])
                    if len(node.args) > 1:
                        default = compact(unparse(node.args[1]), 80)
            if name and name not in seen:
                seen.add(name)
                self.ent("env", name, node, framework="os.environ",
                         fields={"default": default if default is not None else "(required)"})


def extract(path: str, text: str) -> list[Entity]:
    return _Extractor(path, text).run()
