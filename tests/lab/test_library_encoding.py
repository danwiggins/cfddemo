"""Invalid encoded edits retain the last good live interpretation library."""

import pytest

from traceback_lab.web import api as api_module


@pytest.mark.parametrize("attribute", ["LIBRARY_FILE", "LICENSES_FILE"])
def test_invalid_utf8_edit_retains_last_good_and_recovers(attribute, tmp_path, monkeypatch) -> None:
    source = getattr(api_module, attribute)
    valid = source.read_bytes()
    edited = tmp_path / source.name
    edited.write_bytes(valid)
    monkeypatch.setattr(api_module, attribute, edited)
    holder = api_module.LibraryHolder()
    good = holder.get()
    healthy = holder.health()

    edited.write_bytes(valid + b"\n# invalid UTF-8: \xff\n")
    for _ in range(2):  # The unchanged bad edit must keep its error latched.
        assert holder.get() is good
        assert holder.health() == {**healthy, "error": "the library file is not valid UTF-8"}

    edited.write_text(valid.decode("utf-8") + "\n# repaired: caf\u00e9\n", encoding="utf-8")
    recovered = holder.get()
    assert recovered is not good
    assert recovered.version == good.version
    assert holder.health()["error"] is None
    assert holder.get() is recovered
