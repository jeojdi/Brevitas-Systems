import hashlib
import json
from pathlib import Path
from typing import Dict, List, Tuple, Any


class SharedMemoryLayer:
    def __init__(self, persistence_path: str = ""):
        self._chunks: Dict[str, str] = {}
        self._snapshots: Dict[str, Dict[str, Any]] = {}
        self._latest_state_id: str = ""
        self.persistence_path = persistence_path
        self._load_persistent_store()

    def _load_persistent_store(self) -> None:
        if not self.persistence_path:
            return

        path = Path(self.persistence_path)
        if not path.exists():
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._chunks = data.get("chunks", {})
            self._snapshots = data.get("snapshots", {})
            self._latest_state_id = data.get("latest_state_id", "")
        except Exception:
            self._chunks = {}
            self._snapshots = {}
            self._latest_state_id = ""

    def _persist(self) -> None:
        if not self.persistence_path:
            return

        payload = {
            "chunks": self._chunks,
            "snapshots": self._snapshots,
            "latest_state_id": self._latest_state_id,
        }
        path = Path(self.persistence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")

    def _chunk_id(self, text: str) -> str:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        return f"mem:{digest}"

    def store(self, chunks: List[str]) -> List[str]:
        ids = []
        for chunk in chunks:
            chunk_id = self._chunk_id(chunk)
            self._chunks[chunk_id] = chunk
            ids.append(chunk_id)
        self._persist()
        return ids

    def retrieve(self, chunk_ids: List[str]) -> List[str]:
        return [self._chunks[cid] for cid in chunk_ids if cid in self._chunks]

    def materialize_or_reference(self, chunks: List[str]) -> Tuple[List[str], List[str]]:
        references = []
        inline = []
        for chunk in chunks:
            chunk_id = self._chunk_id(chunk)
            if chunk_id in self._chunks:
                references.append(chunk_id)
            else:
                self._chunks[chunk_id] = chunk
                inline.append(chunk)
                references.append(chunk_id)
        self._persist()
        return inline, references

    def save_snapshot(self, task_id: str, values: Dict[str, Any]) -> str:
        payload = {
            "task_id": task_id,
            "values": values,
            "parent": self._latest_state_id,
        }
        state_id = self._chunk_id(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        self._snapshots[state_id] = payload
        self._latest_state_id = state_id
        self._persist()
        return state_id

    def latest_state_id(self) -> str:
        return self._latest_state_id

    def has_state(self, state_id: str) -> bool:
        return state_id in self._snapshots

    def compute_delta(self, base_state_id: str, new_values: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not base_state_id or base_state_id not in self._snapshots:
            return [{"op": "set", "path": key, "value": value} for key, value in new_values.items()]

        base_values = self._snapshots[base_state_id].get("values", {})
        delta_ops = []
        for key, value in new_values.items():
            if key not in base_values or base_values[key] != value:
                delta_ops.append({"op": "set", "path": key, "value": value})
        for key in base_values.keys() - new_values.keys():
            delta_ops.append({"op": "del", "path": key})
        return delta_ops

    def apply_delta(self, base_state_id: str, delta_ops: List[Dict[str, Any]]) -> Dict[str, Any]:
        if base_state_id and base_state_id in self._snapshots:
            result = dict(self._snapshots[base_state_id].get("values", {}))
        else:
            result = {}

        for op in delta_ops:
            path = op.get("path")
            if op.get("op") == "set":
                result[path] = op.get("value")
            elif op.get("op") == "del":
                result.pop(path, None)
        return result
