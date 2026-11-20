"""Privacy filter for everything that leaves the PC (model requests and web searches).

The values to hide are discovered at run time, never written in the code: the home folder path, the
Windows user name, the computer name, the tailnet host name and the tailnet owner's login, plus any
extra terms from config (`private_terms`). They are replaced by neutral placeholders before text is sent
to the model or to a search engine.

The home-folder placeholder is distinctive (`C:\\Users\\snapuser`), so paths the model writes back in tool
calls can be mapped to the real folder again (`restore_paths`). Nothing else is mapped back.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

HOME_PLACEHOLDER_WIN = "C:\\Users\\snapuser"
HOME_PLACEHOLDER_POSIX = "/home/snapuser"
COMMON_WORDS = {"user", "users", "admin", "administrator", "owner", "guest", "test", "home", "root", "default",
                "public", "dev", "pc", "me", "you"}


class Redactor:
    def __init__(self, extra_terms: list[str] | None = None, home: str | None = None, username: str | None = None,
                 computer: str | None = None, secrets: list[str] | None = None, guard=None):
        from .guard import Guard
        self.guard = guard if guard is not None else Guard()
        self.home = home if home is not None else str(Path.home())
        self.username = username if username is not None else (os.environ.get("USERNAME") or os.environ.get("USER") or "")
        self.computer = computer if computer is not None else (os.environ.get("COMPUTERNAME") or "")
        self.extra: list[tuple[str, str]] = []
        self.secrets = [s for s in (secrets or []) if s]
        for t in extra_terms or []:
            self.add_term(t)
        self._build()

    def add_term(self, term: str, placeholder: str | None = None) -> None:
        term = (term or "").strip().rstrip(".")
        if len(term) < 3 or term.lower() in COMMON_WORDS:
            return
        if any(t.lower() == term.lower() for t, _ in self.extra):
            return
        if not placeholder:
            placeholder = "owner@example.com" if "@" in term else ("pc.tailnet.ts.net" if term.endswith(".ts.net") else "[private]")
        self.extra.append((term, placeholder))
        self._build()

    def add_secret(self, value: str | None) -> None:
        if value and value not in self.secrets:
            self.secrets.append(value)

    def _build(self) -> None:
        rules: list[tuple[re.Pattern, str]] = []
        home = self.home.rstrip("\\/")
        if home and len(home) > 3:
            win = "\\" in home
            ph = HOME_PLACEHOLDER_WIN if win else HOME_PLACEHOLDER_POSIX
            variants = {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}
            for v in sorted(variants, key=len, reverse=True):
                rep = ph if v == home else (ph.replace("\\", "/") if "/" in v and "\\" not in v else ph.replace("\\", "\\\\"))
                rules.append((re.compile(re.escape(v), re.I), rep))
        # longer terms first so that a host name is replaced before a bare computer name inside it
        terms = list(self.extra)
        if self.computer and len(self.computer) >= 3 and self.computer.lower() not in COMMON_WORDS:
            terms.append((self.computer, "my-pc"))
        if self.username and len(self.username) >= 4 and self.username.lower() not in COMMON_WORDS:
            terms.append((self.username, "snapuser"))
        for t, ph in sorted(terms, key=lambda x: len(x[0]), reverse=True):
            if re.fullmatch(r"[\w.\-@]+", t):
                rules.append((re.compile(r"(?<![\w])" + re.escape(t) + r"(?![\w])", re.I), ph))
            else:
                rules.append((re.compile(re.escape(t), re.I), ph))
        self._rules = rules

    def clean(self, text: str) -> str:
        """Hide private values in a piece of text that is about to leave the PC."""
        if not text or not isinstance(text, str):
            return text
        text = self.guard.clean(text)
        for s in self.secrets:
            if s in text:
                text = text.replace(s, "***")
        for pat, rep in self._rules:
            text = pat.sub(lambda _m, r=rep: r, text)
        return text

    def clean_obj(self, obj, *, user_data: bool = False):
        """Redact API payload text without renaming protocol fields or literal selectors.

        Metadata and schema example/default values are user data. Tool names and
        schemas are API contracts: their field names and references must stay intact.
        """
        if isinstance(obj, str):
            return self.clean(obj)
        if isinstance(obj, list):
            return [self.clean_obj(x, user_data=user_data) for x in obj]
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                key = self.clean(k) if user_data else k
                if not user_data and k in {"parameters", "inputSchema", "schema"}:
                    result[key] = self._clean_schema(v)
                elif not user_data and k in {"role", "type", "name", "namespace", "tool_choice",
                                             "reasoning_effort"} and isinstance(v, str):
                    result[key] = v
                else:
                    result[key] = self.clean_obj(v, user_data=user_data or k in {
                        "metadata", "default", "const", "enum", "examples"})
            return result
        return obj

    def _clean_values(self, value):
        """Schema examples/defaults can contain argument objects; keep their keys."""
        if isinstance(value, dict):
            return {key: self._clean_values(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self._clean_values(child) for child in value]
        return self.clean(value) if isinstance(value, str) else value

    def _clean_schema(self, schema, *, property_names: bool = False):
        """Clean prose and user values, keeping schema syntax and validation names."""
        if not isinstance(schema, dict):
            return copy.deepcopy(schema)  # Boolean schemas are valid too.
        result = {}
        for key, value in schema.items():
            if key in {"title", "description", "$comment"} and isinstance(value, str):
                result[key] = self.clean(value)
            elif key in {"default", "const", "enum", "examples"}:
                result[key] = copy.deepcopy(value) if property_names else self._clean_values(value)
            elif key in {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas",
                         "dependencies"} and isinstance(value, dict):
                result[key] = {name: self._clean_schema(child, property_names=property_names) for name, child in value.items()}
            elif key in {"allOf", "anyOf", "oneOf", "prefixItems"} and isinstance(value, list):
                result[key] = [self._clean_schema(child, property_names=property_names) for child in value]
            elif key == "items" and isinstance(value, list):  # Older tuple schemas.
                result[key] = [self._clean_schema(child, property_names=property_names) for child in value]
            elif key in {"items", "additionalItems", "additionalProperties", "unevaluatedItems",
                         "unevaluatedProperties", "contains", "propertyNames", "not", "if", "then", "else",
                         "contentSchema"}:
                result[key] = self._clean_schema(value, property_names=property_names or key == "propertyNames")
            else:
                # Includes required/dependentRequired, references, types and patterns.
                result[key] = copy.deepcopy(value)
        return result

    def restore_paths(self, text: str) -> str:
        """Map the home-folder placeholder back to the real folder (used on tool-call arguments only)."""
        if not text or not self.home:
            return text
        home = self.home.rstrip("\\/")
        pairs = [(HOME_PLACEHOLDER_WIN.replace("\\", "\\\\"), home.replace("\\", "\\\\")),   # inside JSON strings
                 (HOME_PLACEHOLDER_WIN, home),
                 (HOME_PLACEHOLDER_WIN.replace("\\", "/"), home.replace("\\", "/")),
                 (HOME_PLACEHOLDER_POSIX, home.replace("\\", "/"))]
        for ph, real in pairs:
            text = re.sub(re.escape(ph), lambda _m, r=real: r, text, flags=re.I)
        return text

    def contains_private(self, text: str, secrets=None) -> bool:
        """Whether clean() would change this text, found without building the cleaned copy. `secrets` is the
        guard's current list when a caller checks many texts at once: each read of it asks Windows Credential
        Manager and the registry, and checking 27,600 files this way took about 5 minutes (2026-11-17)."""
        if not text or not isinstance(text, str):
            return False
        if any(s in text for s in (self.guard.secrets() if secrets is None else secrets)):
            return True
        if any(s in text for s in self.secrets):
            return True
        return any(pattern.search(text) for pattern, _ in self._rules)
