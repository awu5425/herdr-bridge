"""Atomic, process-safe state store for pane <-> hc terminal bindings."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import time
from typing import Iterator


@dataclass(slots=True)
class Binding:
    """Persistent mapping between a Herdr pane and an hc terminal."""

    pane_id: str
    hc_address: str
    workspace_id: str = ""
    tab_id: str = ""
    last_seq: int = 0
    created_ms: int = 0
    updated_ms: int = 0


class BindingStore:
    """Thread/process-safe JSON persistence for pane <-> hc bindings."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(".lock")

    @contextmanager
    def _locked(self, exclusive: bool) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock_file:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(lock_file.fileno(), mode)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_raw(self) -> dict[str, dict]:
        if not self.path.exists():
            return {"bindings": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"bindings": {}}
        if not isinstance(data, dict) or not isinstance(data.get("bindings"), dict):
            return {"bindings": {}}
        return data

    def _write_raw(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, self.path)

    def all_bindings(self) -> list[Binding]:
        with self._locked(exclusive=False):
            raw = self._read_raw()
            result: list[Binding] = []
            for pane_id, item in raw["bindings"].items():
                if not isinstance(item, dict) or "hc_address" not in item:
                    continue
                result.append(
                    Binding(
                        pane_id=pane_id,
                        hc_address=str(item["hc_address"]),
                        workspace_id=str(item.get("workspace_id", "")),
                        tab_id=str(item.get("tab_id", "")),
                        last_seq=int(item.get("last_seq", 0)),
                        created_ms=int(item.get("created_ms", 0)),
                        updated_ms=int(item.get("updated_ms", 0)),
                    )
                )
            return result

    def get_by_pane(self, pane_id: str) -> Binding | None:
        with self._locked(exclusive=False):
            raw = self._read_raw()
            item = raw["bindings"].get(pane_id)
            if not isinstance(item, dict) or "hc_address" not in item:
                return None
            return Binding(
                pane_id=pane_id,
                hc_address=str(item["hc_address"]),
                workspace_id=str(item.get("workspace_id", "")),
                tab_id=str(item.get("tab_id", "")),
                last_seq=int(item.get("last_seq", 0)),
                created_ms=int(item.get("created_ms", 0)),
                updated_ms=int(item.get("updated_ms", 0)),
            )

    def get_by_hc(self, hc_address: str) -> Binding | None:
        for binding in self.all_bindings():
            if binding.hc_address == hc_address:
                return binding
        return None

    def bind(
        self,
        pane_id: str,
        hc_address: str,
        workspace_id: str = "",
        tab_id: str = "",
    ) -> Binding:
        now = int(time.time() * 1000)
        with self._locked(exclusive=True):
            raw = self._read_raw()
            bindings = raw["bindings"]
            # Ensure 1:1 invariant by removing stale pane entries pointing to same hc_address
            stale_panes = [
                pid
                for pid, item in bindings.items()
                if pid != pane_id and isinstance(item, dict) and item.get("hc_address") == hc_address
            ]
            for pid in stale_panes:
                del bindings[pid]

            existing = bindings.get(pane_id) if isinstance(bindings.get(pane_id), dict) else None
            created_ms = int(existing.get("created_ms", now)) if existing else now
            last_seq = int(existing.get("last_seq", 0)) if existing else 0
            binding = Binding(
                pane_id=pane_id,
                hc_address=hc_address,
                workspace_id=workspace_id,
                tab_id=tab_id,
                last_seq=last_seq,
                created_ms=created_ms,
                updated_ms=now,
            )
            entry = asdict(binding)
            entry.pop("pane_id", None)
            bindings[pane_id] = entry
            self._write_raw(raw)
            return binding

    def migrate_pane(
        self,
        old_pane_id: str,
        new_pane_id: str,
        workspace_id: str = "",
        tab_id: str = "",
    ) -> Binding | None:
        now = int(time.time() * 1000)
        with self._locked(exclusive=True):
            raw = self._read_raw()
            bindings = raw["bindings"]
            item = bindings.pop(old_pane_id, None)
            if not isinstance(item, dict) or "hc_address" not in item:
                return None
            binding = Binding(
                pane_id=new_pane_id,
                hc_address=str(item["hc_address"]),
                workspace_id=workspace_id or str(item.get("workspace_id", "")),
                tab_id=tab_id or str(item.get("tab_id", "")),
                last_seq=int(item.get("last_seq", 0)),
                created_ms=int(item.get("created_ms", now)),
                updated_ms=now,
            )
            entry = asdict(binding)
            entry.pop("pane_id", None)
            bindings[new_pane_id] = entry
            self._write_raw(raw)
            return binding

    def unbind_pane(self, pane_id: str) -> Binding | None:
        with self._locked(exclusive=True):
            raw = self._read_raw()
            bindings = raw["bindings"]
            item = bindings.pop(pane_id, None)
            if item is None:
                return None
            self._write_raw(raw)
            if not isinstance(item, dict) or "hc_address" not in item:
                return None
            return Binding(
                pane_id=pane_id,
                hc_address=str(item["hc_address"]),
                workspace_id=str(item.get("workspace_id", "")),
                tab_id=str(item.get("tab_id", "")),
                last_seq=int(item.get("last_seq", 0)),
                created_ms=int(item.get("created_ms", 0)),
                updated_ms=int(item.get("updated_ms", 0)),
            )

    def unbind_hc(self, hc_address: str) -> list[Binding]:
        removed: list[Binding] = []
        with self._locked(exclusive=True):
            raw = self._read_raw()
            bindings = raw["bindings"]
            to_del = [
                pid
                for pid, item in bindings.items()
                if isinstance(item, dict) and item.get("hc_address") == hc_address
            ]
            for pid in to_del:
                item = bindings.pop(pid)
                removed.append(
                    Binding(
                        pane_id=pid,
                        hc_address=str(item["hc_address"]),
                        workspace_id=str(item.get("workspace_id", "")),
                        tab_id=str(item.get("tab_id", "")),
                        last_seq=int(item.get("last_seq", 0)),
                        created_ms=int(item.get("created_ms", 0)),
                        updated_ms=int(item.get("updated_ms", 0)),
                    )
                )
            if to_del:
                self._write_raw(raw)
        return removed

    def next_seq(self, pane_id: str) -> int:
        with self._locked(exclusive=True):
            raw = self._read_raw()
            bindings = raw["bindings"]
            item = bindings.get(pane_id)
            if not isinstance(item, dict):
                return int(time.time() * 1000)
            seq = int(item.get("last_seq", 0)) + 1
            item["last_seq"] = seq
            item["updated_ms"] = int(time.time() * 1000)
            self._write_raw(raw)
            return seq
