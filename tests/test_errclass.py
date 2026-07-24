"""Unit tests for errclass.py -- the permanent-vs-transient classifier."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi import errclass  # noqa: E402


# ----------------------------------------------------------- is_permanent
def test_real_incident_signatures_are_permanent():
    assert errclass.is_permanent(
        "spnego.exceptions.LogonFailure: NT_STATUS_LOGON_FAILURE 0xc000006d")
    assert errclass.is_permanent("NT_STATUS_ACCESS_DENIED")
    assert errclass.is_permanent("PermissionError: [Errno 13] Permission denied")
    assert errclass.is_permanent("no_smb_creds")
    assert errclass.is_permanent("unclassified_nas")


def test_transient_errors_are_not_permanent():
    assert not errclass.is_permanent("TimeoutError: timed out")
    assert not errclass.is_permanent("ConnectionResetError: [Errno 104]")
    assert not errclass.is_permanent("BrokenProcessPool")


def test_none_or_empty_is_never_permanent():
    assert not errclass.is_permanent(None)
    assert not errclass.is_permanent("")


def test_case_insensitive_match():
    assert errclass.is_permanent("nt_status_logon_failure")
    assert errclass.is_permanent("NT_STATUS_LOGON_FAILURE")
    assert errclass.is_permanent("Nt_Status_Logon_Failure")


# --------------------------------------------------------------- classify
def test_classify_ok_and_skipped():
    assert errclass.classify("ok", None) == "ok"
    assert errclass.classify("skipped", None) == "ok"


def test_classify_requeue_is_ok_not_a_fault():
    assert errclass.classify("requeue", "evicted: pressure pause") == "ok"


def test_classify_error_permanent():
    assert errclass.classify("error", "LogonFailure: bad creds") == "permanent"


def test_classify_error_transient():
    assert errclass.classify("error", "TimeoutError") == "transient"


def test_classify_error_with_no_detail_is_transient():
    # We can't prove it's permanent without evidence -- default to transient.
    assert errclass.classify("error", None) == "transient"
    assert errclass.classify("failed", "") == "transient"


# -------------------------------------------------------------- signature
def test_signature_groups_identical_permanent_errors():
    a = errclass.signature("LogonFailure: creds bad for host X")
    b = errclass.signature("spnego.LogonFailure at 10:32:01")
    assert a == b == "logonfailure"


def test_signature_distinguishes_different_permanent_errors():
    a = errclass.signature("NT_STATUS_ACCESS_DENIED")
    b = errclass.signature("PermissionError: nope")
    assert a != b


def test_signature_groups_similar_transient_errors_by_prefix():
    common_prefix = "ConnectionResetError: connection reset by peer talking to "
    assert len(common_prefix) > 40  # sanity: the shared part exceeds the cap
    a = errclass.signature(common_prefix + "host-A")
    b = errclass.signature(common_prefix + "host-B")
    assert a == b  # same first ~40 chars despite differing suffix


def test_signature_none_error():
    assert errclass.signature(None) == "<none>"


# --------------------------------- Fix D: local compute faults don't feed breaker
def test_local_compute_faults_classify_ok_not_transient():
    # The pool's exact timeout labels must NOT trip the breaker -- they are
    # per-clip faults handled by the reaper + poison quarantine, not a
    # shared-dependency signal.
    assert errclass.classify("error", "timeout") == "ok"
    assert errclass.classify("error", "pool_reset") == "ok"
    assert errclass.is_local_compute_fault("timeout")
    assert errclass.is_local_compute_fault("  POOL_RESET  ")  # trimmed + case-insensitive


def test_genuine_dependency_timeout_still_transient():
    # EXACT match only: a real dependency error that merely contains the word
    # must still be classifiable as transient (breaker-eligible).
    assert errclass.classify("error", "connection timeout to 10.0.0.5") == "transient"
    assert errclass.is_local_compute_fault("connection timeout") is False
    assert errclass.classify("error", "socket timed out") == "transient"


def test_local_compute_fault_does_not_shadow_permanent():
    # A permanent (auth) error is still permanent regardless.
    assert errclass.classify("error", "NT_STATUS_LOGON_FAILURE") == "permanent"


def test_smb_access_denied_is_permanent():
    # The exact string the 2026-07-23 live run surfaced: kiroshi could auth but
    # lacked write permission -> STATUS_ACCESS_DENIED / 0xc0000022 (no nt_ prefix).
    err = ("smbprotocol.exceptions.SMBOSError: [Error 0] [NtStatus 0xc0000022] "
           "STATUS_ACCESS_DENIED: '\\host\share\out'")
    assert errclass.is_permanent(err)
    assert errclass.classify("error", err) == "permanent"
