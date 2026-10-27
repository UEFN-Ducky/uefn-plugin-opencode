import base64
import hashlib
import io
import tarfile


def test_candidates_fall_back_to_baseline(oc):
    c = oc("opencode_install").candidates
    assert c("Windows", "AMD64") == ["cli-windows-x64", "cli-windows-x64-baseline"]
    assert c("Windows", "ARM64") == ["cli-windows-arm64", "cli-windows-x64-baseline", "cli-windows-x64"]
    assert c("Darwin", "arm64") == ["cli-darwin-arm64"]
    assert c("Linux", "x86_64") == ["cli-linux-x64", "cli-linux-x64-baseline"]


def test_verify_checks_the_pinned_sha512(oc, tmp_path):
    inst = oc("opencode_install")
    f = tmp_path / "pkg.tgz"
    f.write_bytes(b"opencode")
    good = "sha512-" + base64.b64encode(hashlib.sha512(b"opencode").digest()).decode()
    assert inst._verify(f, good)
    assert not inst._verify(f, "sha512-" + base64.b64encode(hashlib.sha512(b"other").digest()).decode())


def _tgz(path, name, data):
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(f"package/bin/{name}")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


def test_install_unpacks_verified_binary_and_rejects_tampering(oc, tmp_path, monkeypatch):
    inst = oc("opencode_install")
    monkeypatch.setattr(inst, "tools_root", lambda: tmp_path)
    monkeypatch.setattr(inst, "candidates", lambda: ["cli-test"])
    payload = tmp_path / "src.tgz"
    _tgz(payload, inst.exe_name(), b"binary")
    digest = base64.b64encode(hashlib.sha512(payload.read_bytes()).digest()).decode()

    def download(url, dest, _progress):
        assert url.endswith(f"cli-test-{inst.OPENCODE_VERSION}.tgz")
        dest.write_bytes(payload.read_bytes())

    monkeypatch.setattr(inst, "_download", download)
    monkeypatch.setattr(inst, "read_version", lambda b: inst.OPENCODE_VERSION)
    monkeypatch.setitem(inst._INTEGRITY, "cli-test", "sha512-" + base64.b64encode(b"wrong" * 13).decode())
    bad = inst.install()
    assert not bad["ok"] and "checksum" in bad["error"] and not inst.owned_bin()
    monkeypatch.setitem(inst._INTEGRITY, "cli-test", "sha512-" + digest)
    good = inst.install()
    assert good["ok"] and inst.owned_bin() == good["cli_path"]
    assert (tmp_path / inst.OPENCODE_VERSION / inst.exe_name()).read_bytes() == b"binary"


def test_resolve_order_override_then_owned_then_path(oc, tmp_path, monkeypatch):
    inst = oc("opencode_install")
    from backend.agent.coding_agents import base

    monkeypatch.setattr(inst, "tools_root", lambda: tmp_path)
    monkeypatch.setattr(base, "which_cli", lambda name, override="": override and "OVERRIDE")
    monkeypatch.setattr(inst.shutil, "which", lambda name: "PATH-opencode")
    monkeypatch.setattr(inst, "read_version", lambda b: "1.9.0")
    assert inst.resolve_bin("C:/mine/opencode.exe") == "OVERRIDE"
    assert inst.resolve_bin() == ""  # an OpenCode 1 on PATH is not used
    monkeypatch.setattr(inst, "read_version", lambda b: "2.3.0")
    assert inst.resolve_bin() == "PATH-opencode"
    owned = tmp_path / inst.OPENCODE_VERSION / inst.exe_name()
    owned.parent.mkdir(parents=True)
    owned.write_bytes(b"x")
    assert inst.resolve_bin() == str(owned)
