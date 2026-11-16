"""PINs and fingerprints stored on the lock, created and named from Home Assistant.

The lock cannot list its credentials, so HA keeps a table of what it created
(kind, name, member id, hardware id, validity).  Codes made here are not known to
the Tuya cloud, so they don't appear in the Smart Life app.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, time as dtime
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from . import protocol as P
from .session import AnswerMissing, LockSession

_LOGGER = logging.getLogger(__name__)

FIRST_MEMBER = 20          # stay clear of members the Smart Life app may have created
LAST_MEMBER = 100
PIN_MIN, PIN_MAX = 6, 10
TEMP_REPLY = {0: "ok", 1: "failed", 2: "no free slot", 3: "duplicate code"}
WEEKDAY_BITS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


class CredentialManager:
    def __init__(self, hass: HomeAssistant, session: LockSession) -> None:
        self.hass = hass
        self.session = session
        self._store = Store(hass, 1, f"rv_lock.{session.address.replace(':', '').lower()}")
        self.items: list[dict[str, Any]] = []
        self.enrollment: dict[str, Any] | None = None
        self.pending_enrolment: dict[str, Any] | None = None
        self.reserved_members: list[int] = []  # members of enrolments with an unknown outcome
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.items = list(data.get("items", []))
        self.pending_enrolment = data.get("pending_enrolment")
        self.reserved_members = list(data.get("reserved_members", []))

    async def _save(self) -> None:
        await self._store.async_save({"items": self.items, "pending_enrolment": self.pending_enrolment,
                                      "reserved_members": self.reserved_members})

    def snapshot(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.items)

    # ---------------------------------------------------------------- lookups
    def name_for(self, method: str, ident: int) -> str | None:
        kind = {"fingerprint": "fingerprint", "pin": "pin", "temporary_pin": "temporary_pin"}.get(method)
        for it in self.items:
            if it["kind"] == kind and it["hw_id"] == ident:
                return it["name"]
        return None

    def _find(self, name: str, kinds: tuple[str, ...]) -> dict[str, Any]:
        for it in self.items:
            if it["name"].lower() == name.lower() and it["kind"] in kinds:
                return it
        raise HomeAssistantError(f"No {'/'.join(kinds)} named '{name}'")

    def _member_for(self, name: str) -> int:
        for it in self.items:
            if it["name"].lower() == name.lower() and it.get("member"):
                return it["member"]
        used = {it.get("member") for it in self.items}
        if self.pending_enrolment:
            used.add(self.pending_enrolment.get("member"))
        used.update(self.reserved_members)
        for m in range(FIRST_MEMBER, LAST_MEMBER + 1):
            if m not in used:
                return m
        raise HomeAssistantError("No free member ids left on the lock")

    def _check_name_free(self, name: str, kinds: tuple[str, ...]) -> None:
        if any(it["name"].lower() == name.lower() and it["kind"] in kinds for it in self.items):
            raise HomeAssistantError(f"'{name}' already exists; delete it first or pick another name")

    @staticmethod
    def _check_code(code: str) -> str:
        code = str(code).strip()
        if not code.isdigit() or not PIN_MIN <= len(code) <= PIN_MAX:
            raise HomeAssistantError(f"PIN must be {PIN_MIN}-{PIN_MAX} digits")
        return code

    # ------------------------------------------------------------- temporary
    async def create_temporary_pin(self, name: str, code: str, start: datetime, end: datetime,
                                   uses: int = 0, weekdays: list[str] | None = None,
                                   daily_from: dtime | None = None, daily_to: dtime | None = None) -> dict:
        async with self._lock:
            code = self._check_code(code)
            self._check_name_free(name, ("pin", "temporary_pin"))
            if end <= start:
                raise HomeAssistantError("End must be after start")
            if end.timestamp() <= time.time():
                raise HomeAssistantError("End is in the past")
            if bool(daily_from) != bool(daily_to):
                raise HomeAssistantError("Give both daily_from and daily_to")
            if daily_from and daily_from >= daily_to:
                raise HomeAssistantError("daily_from must be before daily_to")
            bits = 0
            for d in weekdays or []:
                bits |= 1 << WEEKDAY_BITS[d.lower()[:3]]
            if daily_from and not bits:
                bits = 0x7F  # hours only apply with a schedule: every day
            if bits and not daily_from:
                daily_from, daily_to = dtime(0, 0), dtime(23, 59)
            fhm = (daily_from.hour, daily_from.minute) if daily_from else (0, 0)
            thm = (daily_to.hour, daily_to.minute) if daily_to else (0, 0)
            validity = P.validity_block(int(start.timestamp()), int(end.timestamp()), bits, fhm, thm)
            now = time.time()
            used = {it["hw_id"] for it in self.items if it["kind"] == "temporary_pin" and it.get("hw_id") is not None
                    and datetime.fromisoformat(it["end"]).timestamp() > now}
            try:
                reply = await self.session.dp_transaction(
                    P.DP_TEMP_CREATE, P.temp_pin_create(code, validity, uses), P.DP_TEMP_CREATE,
                    accept=lambda v: len(v) >= 2 and v[0] not in used, timeout=15)
            except AnswerMissing as err:
                if True:
                    self.items.append({"kind": "temporary_pin", "name": name, "hw_id": None, "state": "unknown",
                                       "end": end.isoformat(), "created": time.time()})
                    await self._save()
                    raise HomeAssistantError(
                        f"{err}. The code may or may not be active on the lock; it expires at the end time.") from err
                raise
            hw_id, result = reply[0], reply[1]
            if result != 0:
                raise HomeAssistantError(f"Lock refused the temporary PIN: {TEMP_REPLY.get(result, result)}")
            item = {"kind": "temporary_pin", "name": name, "hw_id": hw_id, "member": None,
                    "start": start.isoformat(), "end": end.isoformat(), "uses": uses,
                    "weekdays": weekdays or [], "daily_from": daily_from.isoformat() if daily_from else None,
                    "daily_to": daily_to.isoformat() if daily_to else None, "created": time.time()}
            self.items.append(item)
            await self._save()
            _LOGGER.info("RV lock: temporary PIN '%s' stored as slot %d", name, hw_id)
            return dict(item)

    # ------------------------------------------------------------- permanent
    async def create_pin(self, name: str, code: str, admin: bool = False) -> dict:
        async with self._lock:
            code = self._check_code(code)
            self._check_name_free(name, ("pin", "temporary_pin"))
            member = self._member_for(name)
            value = P.method_create(P.METHOD_PASSWORD, member, admin, P.PERMANENT_VALIDITY, code)
            reply = await self.session.dp_transaction(
                P.DP_METHOD_CREATE, value, P.DP_METHOD_CREATE,
                accept=lambda v: len(v) >= 5 and v[0] == P.METHOD_PASSWORD and v[3] == member
                and v[1] in (P.STAGE_DONE, P.STAGE_FAILED, P.STAGE_CANCELLED),
                timeout=15)
            r = P.parse_method_reply(reply)
            if r is None or r["stage"] != P.STAGE_DONE:
                raise HomeAssistantError(f"Lock refused the PIN (stage {reply[1]:#04x})")
            item = {"kind": "pin", "name": name, "hw_id": r["hw_id"], "member": member, "admin": admin,
                    "created": time.time()}
            self.items.append(item)
            await self._save()
            _LOGGER.info("RV lock: PIN '%s' stored as slot %d (member %d)", name, r["hw_id"], member)
            return dict(item)

    async def delete(self, name: str, kind: str) -> dict:
        async with self._lock:
            it = self._find(name, (kind,))
            if it.get("hw_id") is None:
                self.items.remove(it)  # outcome of its creation was unknown; it expires by itself
                await self._save()
                return dict(it)
            if kind == "temporary_pin":
                reply = await self.session.dp_transaction(
                    P.DP_TEMP_DELETE, P.temp_pin_delete(it["hw_id"]), P.DP_TEMP_DELETE,
                    accept=lambda v: len(v) >= 2 and v[0] == it["hw_id"], timeout=15)
                ok = reply[1] in (0, 2)  # 2 = already gone
            else:
                method = P.METHOD_PASSWORD if kind == "pin" else P.METHOD_FINGERPRINT
                member = it["member"] & 0xFF
                reply = await self.session.dp_transaction(
                    P.DP_METHOD_DELETE, P.method_delete(method, it["member"], it.get("admin", False), it["hw_id"]),
                    P.DP_METHOD_DELETE,
                    accept=lambda v: len(v) >= 7 and v[0] == method and v[3] == member and v[4] == it["hw_id"],
                    timeout=15)
                ok = reply[6] in (0xFF, 0x01)  # 0x01 = not found (already gone)
            if not ok:
                # Tuya DP 2 result: 0x00 deletion failed, 0x01 does not exist, 0xFF succeeded.
                raise HomeAssistantError(
                    f"The lock refused to delete '{name}' (result {reply[-1]:#04x}). If the lock was "
                    f"factory-reset, use rv_lock.forget_credential to drop HA's record of it.")
            self.items.remove(it)
            await self._save()
            return dict(it)

    async def forget(self, name: str | None) -> dict:
        """Drop HA's record(s) without talking to the lock (after a factory reset)."""
        async with self._lock:
            if name:
                gone = [self._find(name, ("fingerprint", "pin", "temporary_pin"))]
            else:
                gone = list(self.items)
            for it in gone:
                self.items.remove(it)
            self.pending_enrolment = None
            self.reserved_members = []
            await self._save()
            _LOGGER.info("RV lock: forgot %d stored credential(s)", len(gone))
            return {"forgotten": gone}

    # ------------------------------------------------------------ fingerprint
    async def enroll_fingerprint(self, name: str, admin: bool = False) -> dict:
        async with self._lock:
            self._check_name_free(name, ("fingerprint",))
            member = self._member_for(name)
            self.pending_enrolment = {"name": name, "member": member, "admin": admin}
            await self._save()
            self.enrollment = {"name": name, "stage": "touch the sensor", "touches": 0}
            self.session._notify()

            def progress(v: bytes) -> None:
                r = P.parse_method_reply(v)
                if r and r["type"] == P.METHOD_FINGERPRINT and r["member"] == member and r["stage"] == P.STAGE_PROGRESS:
                    self.enrollment = {"name": name, "stage": "lift and touch again" if r["result"] == 1 else "touch again",
                                       "touches": r["count"]}
                    self.session._notify()

            done_ok = False
            try:
                reply = await self.session.dp_transaction(
                    P.DP_METHOD_CREATE, P.method_create(P.METHOD_FINGERPRINT, member, admin, P.PERMANENT_VALIDITY),
                    P.DP_METHOD_CREATE,
                    accept=lambda v: len(v) >= 5 and v[0] == P.METHOD_FINGERPRINT and v[3] == member
                    and v[1] in (P.STAGE_DONE, P.STAGE_FAILED, P.STAGE_CANCELLED),
                    timeout=90, progress=progress)
                r = P.parse_method_reply(reply)
                if r is None or r["stage"] != P.STAGE_DONE:
                    self.enrollment = {"name": name, "stage": "failed", "touches": 0}
                    raise HomeAssistantError(f"Fingerprint enrolment failed (stage {reply[1]:#04x})")
                item = {"kind": "fingerprint", "name": name, "hw_id": r["hw_id"], "member": member, "admin": admin,
                        "created": time.time()}
                self.items.append(item)
                self.enrollment = {"name": name, "stage": "done", "touches": r["count"]}
                done_ok = True
                return dict(item)
            except (HomeAssistantError, asyncio.CancelledError) as err:
                if not done_ok:
                    if isinstance(err, asyncio.CancelledError):
                        stage = "cancelled"
                    elif self.enrollment and self.enrollment.get("stage") == "failed":
                        stage = "failed"
                    elif isinstance(err, AnswerMissing):
                        stage = "timed out"
                    else:
                        stage = f"error: {err}"
                    self.enrollment = {"name": name, "stage": stage, "touches": 0}
                    await self.cancel_enrolment(member, admin)
                    if member not in self.reserved_members:
                        self.reserved_members.append(member)  # it may have completed on the lock anyway
                raise
            finally:
                self.pending_enrolment = None
                await self._save()
                self.session._notify()

    async def cancel_enrolment(self, member: int, admin: bool) -> None:
        """Best effort: tell the lock to stop enrolling (SDK cancel frame)."""
        try:
            await self.session.dp_transaction(
                P.DP_METHOD_CREATE, P.method_cancel(P.METHOD_FINGERPRINT, member, admin), P.DP_METHOD_CREATE,
                accept=lambda v: True, timeout=3)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("RV lock: enrolment cancel: %r", err)

    async def recover_pending(self) -> None:
        """After a restart mid-enrolment: cancel it on the lock; its member stays reserved."""
        async with self._lock:
            p = self.pending_enrolment
            if not p:
                return
            await self.cancel_enrolment(p["member"], p.get("admin", False))
            if p["member"] not in self.reserved_members:
                self.reserved_members.append(p["member"])
            self.pending_enrolment = None
            await self._save()
