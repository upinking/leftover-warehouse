"""密钥与库存分离：默认仅本次运行；用户选择记住时写入 Mac 钥匙串。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from warehouse import DomainError


class CredentialStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.marker = self.root / "ai-credential-state.json"
        self.account = hashlib.sha256(str(self.root).encode()).hexdigest()
        self.value = None
        self.remembered = self.marker.exists() and json.loads(self.marker.read_text()).get("remembered") is True

    def _keychain(self, action, value=None):
        helper = (Path(sys.executable).parent / "keychain-helper" if getattr(sys, "frozen", False)
                  else Path(__file__).parent / "build" / "keychain-helper")
        if not helper.is_file():
            raise DomainError("钥匙串组件不可用，请取消“记住密钥”，仅在本次运行使用")
        request = {"account": self.account, "action": action}
        if value is not None:
            request["value"] = value
        try:
            result = subprocess.run([str(helper)], input=json.dumps(request).encode(),
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
            body = json.loads(result.stdout)
            if result.returncode or body.get("ok") is not True:
                raise ValueError()
            return body.get("value") or None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise DomainError("无法访问本应用的钥匙串密钥，可选择仅在本次运行使用")

    def _mark(self, remembered):
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.marker.with_suffix(".tmp")
        with temp.open("w") as handle:
            os.chmod(temp, 0o600)
            json.dump({"remembered": remembered}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.marker)
        self.remembered = remembered

    def get(self):
        return self.value or (self._keychain("get") if self.remembered else None)

    def set(self, value, remember=False):
        if not isinstance(value, str) or not value.strip() or len(value) > 16000:
            raise DomainError("API 密钥格式无效")
        if remember:
            self._keychain("set", value)
            self._mark(True)
        else:
            if self.remembered:
                self._keychain("delete")
            self._mark(False)
        self.value = value

    def clear(self):
        if self.remembered:
            self._keychain("delete")
        self._mark(False)
        self.value = None

    def status(self):
        return {"hasKey": bool(self.value or self.remembered),
                "storage": "keychain" if self.remembered else "session" if self.value else "none"}
