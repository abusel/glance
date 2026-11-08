"""TypeScript / JavaScript extraction via tree-sitter.

Understands Express / Fastify / Hono / Koa-style routes, NestJS controllers,
Next.js route handlers and server actions, tRPC procedures, Zod schemas, TS
interfaces / types / enums, Drizzle tables, TypeORM entities, Mongoose schemas,
DBOS workflows / steps / transactions / queues, BullMQ queues and workers,
Knex / TypeORM migrations, config constants, env var reads and every function /
method signature.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import PurePosixPath

from ..model import Entity, short_hash
from . import sql as sqlx

HTTP = {"get", "post", "put", "patch", "delete", "options", "head", "all"}
NEST_HTTP = {"Get", "Post", "Put", "Patch", "Delete", "Options", "Head", "All"}
DRIZZLE_TABLES = {"pgTable", "mysqlTable", "sqliteTable", "table"}
DBOS_DECOS = {"workflow", "step", "transaction", "scheduled", "communicator", "storedProcedure"}
CONST_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,}$")
PROMPT_RE = re.compile(r"PROMPT|INSTRUCTION|SYSTEM_MESSAGE|TEMPLATE", re.I)
ROUTER_OBJ_RE = re.compile(r"^(app|router|api|server|fastify|hono|r|routes?|\w*[Rr]outer|\w*[Aa]pp|\w*[Ss]erver)$")


@lru_cache(maxsize=2)
def _parser(tsx: bool):
    from tree_sitter import Language, Parser
    import tree_sitter_typescript as tst

    return Parser(Language(tst.language_tsx() if tsx else tst.language_typescript()))


def compact(s: str, limit: int = 220) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def txt(n) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def line(n) -> int:
    return n.start_point[0] + 1


def end_line(n) -> int:
    return n.end_point[0] + 1


def norm_hash(n) -> str:
    """Formatting- and comment-insensitive hash of a subtree."""
    toks: list[str] = []
    stack = [n]
    while stack:
        cur = stack.pop()
        if cur.type == "comment":
            continue
        if cur.child_count == 0:
            toks.append(txt(cur))
        else:
            stack.extend(reversed(cur.children))
    return short_hash(" ".join(toks))


def str_value(n) -> str | None:
    if n is None:
        return None
    if n.type == "string":
        return txt(n)[1:-1]
    if n.type == "template_string":
        return txt(n)[1:-1]
    return None


def callee(call) -> str:
    f = call.child_by_field_name("function")
    return dotted(f)


def dotted(n) -> str:
    if n is None:
        return ""
    if n.type in ("identifier", "property_identifier", "type_identifier", "this"):
        return txt(n)
    if n.type == "member_expression":
        return f"{dotted(n.child_by_field_name('object'))}.{txt(n.child_by_field_name('property'))}"
    if n.type == "call_expression":
        return dotted(n.child_by_field_name("function")) + "()"
    if n.type == "parenthesized_expression" and n.named_children:
        return dotted(n.named_children[0])
    return ""


def chain_root(call):
    """For a.b().c().d() return the innermost call (a.b())."""
    cur = call
    while cur is not None and cur.type == "call_expression":
        f = cur.child_by_field_name("function")
        if f is not None and f.type == "member_expression":
            obj = f.child_by_field_name("object")
            if obj is not None and obj.type == "call_expression":
                cur = obj
                continue
        break
    return cur


def chain_calls(call) -> list:
    out = []
    cur = call
    while cur is not None and cur.type == "call_expression":
        out.append(cur)
        f = cur.child_by_field_name("function")
        cur = f.child_by_field_name("object") if f is not None and f.type == "member_expression" else None
    return out


def args_of(call) -> list:
    a = call.child_by_field_name("arguments")
    return [c for c in a.named_children if c.type != "comment"] if a is not None else []


def object_pairs(obj) -> dict[str, object]:
    out: dict[str, object] = {}
    if obj is None or obj.type != "object":
        return out
    for c in obj.named_children:
        if c.type == "pair":
            k = c.child_by_field_name("key")
            key = str_value(k) if k is not None and k.type == "string" else txt(k)
            out[key] = c.child_by_field_name("value")
        elif c.type == "shorthand_property_identifier":
            out[txt(c)] = c
        elif c.type == "method_definition":
            out[txt(c.child_by_field_name("name"))] = c
    return out


def is_literal(n) -> bool:
    if n is None:
        return False
    if n.type in ("number", "string", "true", "false", "null", "undefined"):
        return True
    if n.type == "template_string":
        return not any(c.type == "template_substitution" for c in n.children)
    if n.type == "unary_expression":
        return all(is_literal(c) for c in n.named_children)
    if n.type == "binary_expression":
        return is_literal(n.child_by_field_name("left")) and is_literal(n.child_by_field_name("right"))
    if n.type in ("array", "object"):
        kids = [c for c in n.named_children if c.type != "comment"]
        return all(is_literal(c.child_by_field_name("value") if c.type == "pair" else c) for c in kids)
    if n.type in ("as_expression", "satisfies_expression"):
        return is_literal(n.named_children[0]) if n.named_children else False
    if n.type == "parenthesized_expression":
        return all(is_literal(c) for c in n.named_children)
    return False


def preceding_decorators(n) -> list:
    """Decorators attached to a class member appear as preceding siblings in class_body."""
    out = []
    prev = n.prev_named_sibling
    while prev is not None and prev.type == "decorator":
        out.insert(0, prev)
        prev = prev.prev_named_sibling
    out += [c for c in n.children if c.type == "decorator"]
    return out


def deco_call(d):
    """Return (dotted name, call node or None) for a decorator."""
    inner = d.named_children[0] if d.named_children else None
    if inner is None:
        return "", None
    if inner.type == "call_expression":
        return callee(inner), inner
    return dotted(inner), None


def next_route_path(path: str) -> str | None:
    p = PurePosixPath(path)
    parts = list(p.parts)
    if p.stem == "route" and "app" in parts:
        segs = parts[parts.index("app") + 1 : -1]
    elif "pages" in parts and "api" in parts[parts.index("pages"):]:
        i = parts.index("pages")
        segs = parts[i + 1 :]
        segs[-1] = PurePosixPath(segs[-1]).stem
        if segs[-1] == "index":
            segs = segs[:-1]
    else:
        return None
    out = []
    for s in segs:
        if s.startswith("(") and s.endswith(")") or s.startswith("@"):
            continue
        s = re.sub(r"^\[\[?\.\.\.(\w+)\]?\]$", r"*\1", s)
        s = re.sub(r"^\[(\w+)\]$", r":\1", s)
        out.append(s)
    return "/" + "/".join(out)


class _Extractor:
    def __init__(self, path: str, text: str, category: str):
        self.path = path
        self.text = text
        self.category = category
        tsx = path.endswith((".tsx", ".jsx", ".js", ".mjs", ".cjs"))
        self.tree = _parser(tsx).parse(text.encode("utf-8"))
        self.root = self.tree.root_node
        self.entities: list[Entity] = []
        self.next_path = next_route_path(path)
        self.use_server = bool(re.match(r"\s*(['\"])use server\1", text))

    def ent(self, kind: str, name: str, node, **kw) -> Entity:
        e = Entity(kind=kind, name=name, file=self.path, line=line(node), end_line=end_line(node), lang="typescript", **kw)
        self.entities.append(e)
        return e

    def run(self) -> list[Entity]:
        if self.root.has_error and self._error_ratio() > 0.2:
            raise SyntaxError("too many parse errors")
        if self.category == "migration":
            self._migration()
        for top in self.root.named_children:
            self._top(top)
        self._walk_calls(self.root)
        self._env(self.root)
        return self.entities

    def _error_ratio(self) -> float:
        errs = 0
        stack = [self.root]
        total = 0
        while stack:
            n = stack.pop()
            total += 1
            if n.type == "ERROR" or n.is_missing:
                errs += 1
            stack.extend(n.children)
        return errs / max(total, 1)

    # ------------------------------------------------------------------ top-level declarations
    def _top(self, node, exported: bool = False, class_decos: list | None = None) -> None:
        t = node.type
        if t == "export_statement":
            decos = [c for c in node.children if c.type == "decorator"]
            decl = node.child_by_field_name("declaration")
            if decl is None:
                for c in node.named_children:
                    if c.type in ("class_declaration", "function_declaration", "lexical_declaration", "interface_declaration",
                                  "type_alias_declaration", "enum_declaration", "abstract_class_declaration", "class"):
                        decl = c
            if decl is not None:
                self._top(decl, exported=True, class_decos=decos)
            return
        if t in ("class_declaration", "abstract_class_declaration"):
            self._class(node, (class_decos or []) + [c for c in node.children if c.type == "decorator"])
        elif t in ("function_declaration", "generator_function_declaration"):
            name = txt(node.child_by_field_name("name"))
            self._function(node, name, exported)
        elif t in ("lexical_declaration", "variable_declaration"):
            for d in node.named_children:
                if d.type == "variable_declarator":
                    self._declarator(d, exported)
        elif t == "interface_declaration":
            name = txt(node.child_by_field_name("name"))
            self._type_fields(node, name, node.child_by_field_name("body"), "interface")
        elif t == "type_alias_declaration":
            name = txt(node.child_by_field_name("name"))
            val = node.child_by_field_name("value")
            if val is not None and val.type == "object_type":
                self._type_fields(node, name, val, "type")
            elif val is not None and val.type == "union_type" and re.search(r"['\"]", txt(val)):
                self.ent("type", name, node, framework="ts-union", signature=compact(txt(val), 200),
                         fields={m.strip(): "" for m in txt(val).split("|") if m.strip()}, body_hash=norm_hash(val))
        elif t == "enum_declaration":
            name = txt(node.child_by_field_name("name"))
            body = node.child_by_field_name("body")
            fields = {}
            for m in (body.named_children if body is not None else []):
                if m.type == "enum_assignment":
                    fields[txt(m.child_by_field_name("name"))] = txt(m.child_by_field_name("value"))
                elif m.type in ("property_identifier", "string"):
                    fields[txt(m)] = ""
            self.ent("type", name, node, framework="ts-enum", fields=fields, signature=f"enum {name}")
        elif t == "expression_statement":
            pass  # handled in _walk_calls

    def _type_fields(self, node, name: str, body, kind_label: str) -> None:
        fields: dict[str, str] = {}
        for m in (body.named_children if body is not None else []):
            if m.type in ("property_signature", "method_signature"):
                n = m.child_by_field_name("name")
                opt = "?" if any(c.type == "?" for c in m.children) else ""
                ta = m.child_by_field_name("type")
                if m.type == "method_signature":
                    fields[txt(n) + opt] = compact(txt(m)[len(txt(n)):], 160)
                else:
                    fields[txt(n) + opt] = compact(txt(ta).lstrip(":").strip(), 160)
            elif m.type == "index_signature":
                fields[compact(txt(m), 60)] = ""
        ext = ""
        for c in node.children:
            if c.type in ("extends_type_clause", "extends_clause"):
                ext = " " + txt(c)
        self.ent("type", name, node, framework="ts-" + kind_label, fields=fields, signature=f"{kind_label} {name}{ext}")

    # ------------------------------------------------------------------ variables
    def _declarator(self, d, exported: bool) -> None:
        name_node = d.child_by_field_name("name")
        name = txt(name_node)
        val = d.child_by_field_name("value")
        if val is None or name_node is None or name_node.type != "identifier":
            return
        while val.type in ("as_expression", "satisfies_expression", "parenthesized_expression") and val.named_children:
            val = val.named_children[0]
        decl = d.parent if d.parent is not None else d
        if val.type in ("arrow_function", "function_expression", "function"):
            self._function(val, name, exported, decl_node=decl)
            if self.use_server and exported:
                self.ent("route", f"action {name}", decl, framework="next-action", signature=f"server action {name}()",
                         fields=self._params(val), meta={"handler": name})
            return
        if val.type == "call_expression":
            self._call_decl(name, val, decl)
            return
        if val.type == "new_expression":
            ctor = dotted(val.child_by_field_name("constructor"))
            a = val.child_by_field_name("arguments")
            argv = [c for c in a.named_children] if a is not None else []
            short = ctor.split(".")[-1]
            if short == "Schema" and argv and argv[0].type == "object":
                self._object_entity("table", name, decl, argv[0], "mongoose", f"new Schema() → {name}")
            elif short in ("Queue", "Worker", "WorkflowQueue", "FlowProducer"):
                qn = str_value(argv[0]) if argv else None
                opts = {}
                for c in argv[1:]:
                    if c.type == "object":
                        opts = {k: compact(txt(v), 80) for k, v in object_pairs(c).items()}
                fw = "dbos" if short == "WorkflowQueue" else "bullmq"
                self.ent("task", f"{short}:{qn or name}", decl, framework=fw, signature=f"new {short}({qn!r})",
                         fields=opts, body_hash=norm_hash(val))
            return
        if CONST_RE.match(name) and is_literal(val):
            sval = str_value(val)
            if sval is not None and (PROMPT_RE.search(name) or len(sval) > 280):
                self.ent("agent", name, decl, framework="prompt", fields={"text": sval}, body_hash=short_hash(sval),
                         signature=compact(sval, 90))
            else:
                self.ent("config", name, decl, framework="constant", signature=compact(txt(val), 160), body_hash=norm_hash(val))

    def _call_decl(self, name: str, call, decl) -> None:
        root = chain_root(call)
        rname = callee(root)
        short = rname.split(".")[-1]
        argv = args_of(root)
        full_text = txt(call)
        # Drizzle tables
        if short in DRIZZLE_TABLES and argv and str_value(argv[0]) is not None and len(argv) > 1 and argv[1].type == "object":
            tname = str_value(argv[0])
            e = self._object_entity("table", name, decl, argv[1], "drizzle", f"{short}(\"{tname}\") → {name}")
            e.meta["tablename"] = tname
            if len(argv) > 2:
                e.meta["table_args"] = compact(txt(argv[2]), 200)
            return
        # Zod schemas
        if rname in ("z.object", "z.strictObject", "z.looseObject") and argv and argv[0].type == "object":
            e = self._object_entity("model", name, decl, argv[0], "zod", f"z.object → {name}")
            mods = [txt(c.child_by_field_name("function").child_by_field_name("property"))
                    for c in chain_calls(call)[:-1] if c.child_by_field_name("function").type == "member_expression"]
            if mods:
                e.meta["modifiers"] = mods
                e.signature += " ." + "().".join(reversed(mods)) + "()"
            return
        if rname.startswith("z.") or (short in ("extend", "merge", "pick", "omit", "partial") and "z." in full_text):
            fields = {}
            obj = next((a for a in argv if a.type == "object"), None)
            if obj is not None:
                fields = {k: compact(txt(v), 160) for k, v in object_pairs(obj).items()}
            self.ent("model", name, decl, framework="zod", signature=compact(full_text, 160) if not fields else f"{compact(rname, 60)} → {name}",
                     fields=fields, body_hash=norm_hash(call))
            return
        # DBOS functional API
        if rname in ("DBOS.registerWorkflow", "DBOS.registerStep", "DBOS.registerTransaction", "DBOS.registerScheduled"):
            target = argv[0] if argv else None
            self.ent("task", name, decl, framework="dbos", signature=f"{rname}({txt(target)[:60]})",
                     meta={"decorator": rname.split("register")[-1].lower()}, body_hash=norm_hash(call))
            if target is not None and target.type in ("arrow_function", "function_expression"):
                self._function(target, name, True, decl_node=decl)
            return
        # tRPC router: name = t.router({...}) handled via pairs
        if short == "router" and argv and argv[0].type == "object":
            for key, v in object_pairs(argv[0]).items():
                if v is not None and v.type == "call_expression":
                    names = [txt(c.child_by_field_name("function").child_by_field_name("property"))
                             for c in chain_calls(v) if c.child_by_field_name("function").type == "member_expression"]
                    kind = next((n for n in names if n in ("query", "mutation", "subscription")), None)
                    if kind:
                        inp = next((c for c in chain_calls(v) if c.child_by_field_name("function").type == "member_expression"
                                    and txt(c.child_by_field_name("function").child_by_field_name("property")) == "input"), None)
                        proc_root = dotted(chain_root(v).child_by_field_name("function")).split(".")[0]
                        self.ent("route", f"trpc {kind} {name}.{key}", v.parent, framework="trpc",
                                 signature=f"{kind} {name}.{key}",
                                 fields={"input": compact(txt(args_of(inp)[0]), 200)} if inp is not None and args_of(inp) else {},
                                 meta={"dependencies": [proc_root], "path": f"{name}.{key}", "methods": [kind.upper()]},
                                 body_hash=norm_hash(v))
            return
        # Pydantic-AI-like / LLM agent constructors
        if short in ("Agent", "createAgent", "generateText", "streamText"):
            obj = next((a for a in argv if a.type == "object"), None)
            fields = {k: compact(txt(v), 300) for k, v in object_pairs(obj).items()} if obj is not None else {}
            self.ent("agent", name, decl, framework="agent", fields=fields, signature=compact(f"{rname}(…)", 80), body_hash=norm_hash(call))

    def _object_entity(self, kind: str, name: str, node, obj, framework: str, sig: str) -> Entity:
        fields = {k: compact(txt(v) if v is not None else "", 200) for k, v in object_pairs(obj).items()}
        return self.ent(kind, name, node, framework=framework, fields=fields, signature=sig, body_hash=norm_hash(obj))

    # ------------------------------------------------------------------ functions & classes
    def _params(self, fn) -> dict[str, str]:
        params = fn.child_by_field_name("parameters")
        out: dict[str, str] = {}
        if params is None:
            p = fn.child_by_field_name("parameter")
            if p is not None:
                out[txt(p)] = "any"
            return out
        for p in params.named_children:
            if p.type in ("required_parameter", "optional_parameter"):
                pat = p.child_by_field_name("pattern")
                ta = p.child_by_field_name("type")
                val = p.child_by_field_name("value")
                key = compact(txt(pat), 60) + ("?" if p.type == "optional_parameter" else "")
                desc = txt(ta).lstrip(":").strip() if ta is not None else "any"
                decos = [txt(c) for c in p.children if c.type == "decorator"]
                if decos:
                    desc = " ".join(decos) + " " + desc
                if val is not None:
                    desc += " = " + txt(val)
                out[key] = compact(desc, 160)
            elif p.type in ("identifier", "rest_pattern", "assignment_pattern", "object_pattern"):
                out[compact(txt(p), 60)] = "any"
        return out

    def _signature(self, fn, name: str) -> str:
        params = fn.child_by_field_name("parameters") or fn.child_by_field_name("parameter")
        ret = fn.child_by_field_name("return_type")
        is_async = any(c.type == "async" for c in fn.children)
        sig = f"{'async ' if is_async else ''}{name}{compact(txt(params), 300) if params is not None and params.type == 'formal_parameters' else '(' + txt(params) + ')'}"
        if ret is not None:
            sig += txt(ret)
        return compact(sig, 400)

    def _function(self, fn, name: str, exported: bool, decl_node=None, owner: str | None = None) -> None:
        node = decl_node or fn
        qual = f"{owner}.{name}" if owner else name
        body = fn.child_by_field_name("body")
        e = self.ent("function", qual, node, signature=self._signature(fn, name), body_hash=norm_hash(body) if body is not None else "",
                     meta={"exported": exported, "lines": end_line(node) - line(node) + 1})
        if self.next_path and exported and name in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            self.ent("route", f"{name} {self.next_path}", node, framework="next", signature=f"{name} {self.next_path} → {name}()",
                     fields=self._params(fn), meta={"methods": [name], "path": self.next_path, "handler": qual, "dependencies": []})
        if self.use_server and exported and not decl_node:
            self.ent("route", f"action {name}", node, framework="next-action", signature=f"server action {name}()",
                     fields=self._params(fn), meta={"handler": qual})
        return e

    def _class(self, node, decos: list) -> None:
        cname = txt(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        cdecos = {deco_call(d)[0]: deco_call(d)[1] for d in decos}
        is_entity = any(k.split(".")[-1] in ("Entity", "ViewEntity") for k in cdecos)
        controller = next((v for k, v in cdecos.items() if k.split(".")[-1] == "Controller"), "missing")
        class_guards = [compact(txt(c), 80) for k, c in cdecos.items() if k.split(".")[-1] in ("UseGuards", "Roles", "Public", "Auth")]

        if is_entity:
            fields = {}
            for m in (body.named_children if body is not None else []):
                if m.type == "public_field_definition":
                    n = m.child_by_field_name("name")
                    ta = m.child_by_field_name("type")
                    ds = " ".join(compact(txt(d), 120) for d in preceding_decorators(m))
                    fields[txt(n)] = compact(f"{txt(ta).lstrip(':').strip()} {ds}".strip(), 220)
            ent_call = next((v for k, v in cdecos.items() if k.split(".")[-1] == "Entity"), None)
            tname = str_value(args_of(ent_call)[0]) if ent_call is not None and args_of(ent_call) else None
            e = self.ent("table", cname, node, framework="typeorm", fields=fields, signature=f"@Entity({tname or ''}) {cname}")
            if tname:
                e.meta["tablename"] = tname

        prefix = ""
        if controller != "missing":
            if controller is not None and args_of(controller):
                prefix = str_value(args_of(controller)[0]) or ""
            prefix = "/" + prefix.strip("/") if prefix else ""

        for m in (body.named_children if body is not None else []):
            if m.type != "method_definition":
                continue
            mname = txt(m.child_by_field_name("name"))
            self._function(m, mname, True, owner=cname)
            mdecos = [deco_call(d) for d in preceding_decorators(m)]
            for dn, call in mdecos:
                last = dn.split(".")[-1]
                if controller != "missing" and last in NEST_HTTP:
                    sub = str_value(args_of(call)[0]) if call is not None and args_of(call) else ""
                    full = (prefix + ("/" + sub.strip("/") if sub else "")) or "/"
                    guards = class_guards + [
                        compact(d2 + "(" + ", ".join(txt(a) for a in args_of(c2)) + ")", 80)
                        for d2, c2 in mdecos
                        if c2 is not None and d2.split(".")[-1] in ("UseGuards", "Roles", "Public", "Auth")
                    ]
                    self.ent("route", f"{last.upper()} {full}", m, framework="nest", signature=f"{last.upper()} {full} → {cname}.{mname}()",
                             fields=self._params(m), meta={"methods": [last.upper()], "path": full, "handler": f"{cname}.{mname}", "dependencies": guards})
                if dn.startswith("DBOS.") and last in DBOS_DECOS:
                    opts = compact(", ".join(txt(a) for a in args_of(call)), 120) if call is not None else ""
                    self.ent("task", f"{cname}.{mname}", m, framework="dbos", signature=f"@DBOS.{last}({opts}) {cname}.{mname}",
                             fields=self._params(m), meta={"decorator": last, "options": opts})

    # ------------------------------------------------------------------ calls anywhere (routes, queues)
    def _walk_calls(self, root) -> None:
        stack = [root]
        while stack:
            n = stack.pop()
            if n.type == "call_expression":
                self._maybe_route(n)
            stack.extend(n.named_children)

    def _maybe_route(self, call) -> None:
        f = call.child_by_field_name("function")
        if f is None or f.type != "member_expression":
            return
        method = txt(f.child_by_field_name("property"))
        obj = f.child_by_field_name("object")
        argv = args_of(call)
        # fastify.route({ method, url, handler })
        if method == "route" and argv and argv[0].type == "object" and obj is not None and obj.type == "identifier":
            pairs = object_pairs(argv[0])
            url = str_value(pairs.get("url")) if pairs.get("url") is not None else None
            if url:
                m = pairs.get("method")
                methods = [s.strip("'\"") for s in re.findall(r"['\"]\w+['\"]", txt(m))] if m is not None else ["ANY"]
                deps = [compact(txt(pairs[k]), 80) for k in ("preHandler", "onRequest", "preValidation") if pairs.get(k) is not None]
                h = pairs.get("handler")
                self.ent("route", f"{'|'.join(x.upper() for x in methods)} {url}", call, framework="fastify",
                         signature=f"{'|'.join(x.upper() for x in methods)} {url}",
                         meta={"methods": [x.upper() for x in methods], "path": url, "dependencies": deps,
                               "handler": txt(h) if h is not None and h.type == "identifier" else "inline",
                               "schema": compact(txt(pairs["schema"]), 200) if pairs.get("schema") is not None else ""},
                         body_hash=norm_hash(h) if h is not None and h.type != "identifier" else "")
            return
        if method not in HTTP or not argv:
            return
        path = str_value(argv[0])
        prefix = ""
        if path is None:
            # router.route('/x').get(handler)
            if obj is not None and obj.type == "call_expression" and callee(obj).endswith(".route"):
                a2 = args_of(obj)
                prefix = str_value(a2[0]) if a2 else None
                if prefix is None:
                    return
                path, handler_args = prefix, argv
            else:
                return
        else:
            if not (path.startswith("/") or path == "*") or obj is None:
                return
            if obj.type == "identifier" and not ROUTER_OBJ_RE.match(txt(obj)):
                return
            if obj.type not in ("identifier", "member_expression", "this"):
                return
            handler_args = argv[1:]
        if not handler_args:
            return
        handler = handler_args[-1]
        middle = handler_args[:-1]
        deps: list[str] = []
        schema = ""
        for mw in middle:
            if mw.type == "object":
                pairs = object_pairs(mw)
                for k in ("preHandler", "onRequest", "preValidation"):
                    if pairs.get(k) is not None:
                        deps.append(compact(txt(pairs[k]), 80))
                if pairs.get("schema") is not None:
                    schema = compact(txt(pairs["schema"]), 200)
            elif mw.type == "array":
                deps += [compact(txt(c), 80) for c in mw.named_children]
            else:
                deps.append(compact(txt(mw), 80))
        is_inline = handler.type in ("arrow_function", "function_expression", "function")
        router = txt(obj) if obj is not None and obj.type == "identifier" else dotted(obj)
        verb = method.upper()
        meta = {"methods": [verb], "path": path, "dependencies": deps, "router": router,
                "handler": "inline" if is_inline else compact(txt(handler), 80)}
        if schema:
            meta["schema"] = schema
        fw = "express"
        if re.search(r"\bfastify\b", self.text):
            fw = "fastify"
        elif re.search(r"from ['\"]hono", self.text):
            fw = "hono"
        elif re.search(r"koa-router|@koa/router", self.text):
            fw = "koa"
        name = f"{verb} {path}"
        if any(e.kind == "route" and e.name == name for e in self.entities):
            name = f"{verb} {path} ({router})"
        self.ent("route", name, call, framework=fw, signature=f"{verb} {path}" + (f"  [{', '.join(deps)}]" if deps else ""),
                 fields=self._params(handler) if is_inline else {}, meta=meta,
                 body_hash=norm_hash(handler.child_by_field_name("body")) if is_inline and handler.child_by_field_name("body") is not None else "")

    # ------------------------------------------------------------------ env
    def _env(self, root) -> None:
        seen: set[str] = set()
        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.named_children)
            name = None
            if n.type == "member_expression":
                o = dotted(n.child_by_field_name("object"))
                if o in ("process.env", "Bun.env", "import.meta.env", "Deno.env"):
                    name = txt(n.child_by_field_name("property"))
            elif n.type == "subscript_expression":
                o = dotted(n.child_by_field_name("object"))
                if o in ("process.env", "Bun.env", "import.meta.env"):
                    name = str_value(n.child_by_field_name("index"))
            elif n.type == "call_expression" and callee(n) == "Deno.env.get":
                a = args_of(n)
                name = str_value(a[0]) if a else None
            if not name or name in seen:
                continue
            p = n.parent
            if p is not None and p.type == "assignment_expression" and p.child_by_field_name("left") == n:
                continue                    # `process.env.X = ...` sets it (tests, scripts); it isn't a read
            seen.add(name)
            default = "(required)"
            if p is not None and p.type == "binary_expression":
                op = p.child_by_field_name("operator")
                ops = txt(op) if op is not None else ""
                if ops in ("??", "||") and p.child_by_field_name("left") == n:
                    default = compact(txt(p.child_by_field_name("right")), 80)
                elif ops in ("===", "==", "!==", "!="):
                    # a feature flag: unset simply means "not equal", so it can't break a deploy
                    other = p.child_by_field_name("right") if p.child_by_field_name("left") == n else p.child_by_field_name("left")
                    lit = str_value(other) if other is not None else None
                    if lit is None:
                        default = "(compared)"
                    elif ops.startswith("!"):
                        default = f"flag: on unless set to {lit!r}"
                    else:
                        default = f"flag: off unless set to {lit!r}"
            self.ent("env", name, n, framework="process.env", fields={"default": default})

    # ------------------------------------------------------------------ migrations (Knex / TypeORM)
    def _migration(self) -> None:
        up_fn = down_fn = None
        stack = [self.root]
        while stack:
            n = stack.pop()
            stack.extend(n.named_children)
            nm = None
            if n.type in ("function_declaration", "method_definition"):
                nm = txt(n.child_by_field_name("name"))
            elif n.type == "variable_declarator":
                nm = txt(n.child_by_field_name("name"))
            elif n.type == "assignment_expression":
                nm = txt(n.child_by_field_name("left")).split(".")[-1]
            if nm == "up" and up_fn is None:
                up_fn = n
            elif nm == "down" and down_fn is None:
                down_fn = n
        if up_fn is None:
            return
        up_ops = self._migration_ops(up_fn)
        down_ops = self._migration_ops(down_fn) if down_fn is not None else []
        name = PurePosixPath(self.path).stem
        fw = "typeorm" if "queryRunner" in self.text or "MigrationInterface" in self.text else "knex"
        e = self.ent("migration", name, self.root, framework=fw, signature=name,
                     meta={"upgrade": up_ops, "downgrade": down_ops, "downgrade_empty": not down_ops, "message": name})
        e.line, e.end_line = 1, len(self.text.splitlines())

    def _migration_ops(self, fn) -> list[dict]:
        ops: list[dict] = []
        body_txt = txt(fn)
        base = line(fn)
        # raw SQL strings (TypeORM queryRunner.query / knex.raw)
        stack = [fn]
        while stack:
            n = stack.pop()
            stack.extend(n.named_children)
            if n.type in ("string", "template_string"):
                s = str_value(n) or ""
                if re.match(r"\s*(CREATE|ALTER|DROP|UPDATE|DELETE|INSERT|RENAME|TRUNCATE)\b", s, re.I):
                    for op in sqlx.parse_ops(s):
                        op["line"] = line(n)
                        ops.append(op)
        # knex schema builder
        for m in re.finditer(r"\.(createTable|dropTable|dropTableIfExists|alterTable|table|renameTable|createTableIfNotExists)\(\s*['\"`](\w+)", body_txt):
            op = {"createTable": "create_table", "createTableIfNotExists": "create_table", "dropTable": "drop_table",
                  "dropTableIfExists": "drop_table", "alterTable": "alter_table", "table": "alter_table", "renameTable": "rename_table"}[m.group(1)]
            seg = body_txt[m.end(): m.end() + 1500]
            seg = seg.split(".createTable(")[0].split(".alterTable(")[0].split(".dropTable(")[0]
            ops.append({"op": op, "table": m.group(2), "column": None,
                        "detail": compact("; ".join(re.findall(r"\b\w+\.\w+\([^;\n]*", seg))[:400], 300),
                        "line": base + body_txt[: m.start()].count("\n")})
            for c in re.finditer(r"\.(dropColumn|dropColumns|renameColumn)\(\s*['\"`](\w+)", seg):
                ops.append({"op": "drop_column" if c.group(1).startswith("drop") else "alter_column", "table": m.group(2),
                            "column": c.group(2), "detail": c.group(0), "line": base + body_txt[: m.start()].count("\n")})
            if op == "alter_table":
                for c in re.finditer(r"\w+\.(\w+)\(\s*['\"`](\w+)['\"`][^\n;]*", seg):
                    if c.group(1) in ("dropColumn", "dropColumns", "renameColumn", "dropForeign", "dropIndex", "index", "unique", "foreign", "primary"):
                        continue
                    ops.append({"op": "add_column", "table": m.group(2), "column": c.group(2), "detail": compact(c.group(0), 200),
                                "line": base + body_txt[: m.start()].count("\n")})
        return ops


def extract(path: str, text: str, category: str = "source") -> list[Entity]:
    return _Extractor(path, text, category).run()
