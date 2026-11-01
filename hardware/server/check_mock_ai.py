"""mock 데이터 → AI 엔진 → 서버 → API 확인 — 파이프라인이 끝까지 도는지 본다

mock 시나리오(실제 수집 분포로 만든 센서 값)를 AI 판정 엔진(--engine ai)에 넣고,
기준 자세 측정 → 판정 → 착석 구간 → DB → 리포트 API까지 이어지는지 확인한다.
통과 기준은 '끝까지 돈다'이다. AI 판정이 시나리오 라벨과 같은지는 참고로만 보여 준다
(mock 압력은 실제 수집값을 흉내 낸 것이라 AI가 항상 같은 답을 내지는 않는다).

사용 예 (저장소 최상위):
    python -m hardware.server.check_mock_ai
    python -m hardware.server.check_mock_ai --scenario cross_left lean_right
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from common.config import ConfigError, load_config
from hardware.mock.generator import step_labels
from hardware.mock.scenarios import SCENARIOS
from hardware.server.check_session import APP_START, Checker, FakeTime
from hardware.server.clock import AppClock
from hardware.server.mock_server import create_app

DT = 0.5
DEFAULT = ["normal", "lean_left", "lean_right", "cross_left", "cross_right", "forward_head", "tilt_left",
           "tilt_right", "empty", "demo"]
VALID = {"seat": {"normal", "lean_left", "lean_right", "cross_left", "cross_right", "empty", "unknown"},
         "head": {"normal", "forward", "unknown"}, "tilt": {"none", "left", "right", "unknown"}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", nargs="*", default=DEFAULT)
    args = ap.parse_args()
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        return 1
    ok = Checker()
    info: list[str] = []

    for name in args.scenario:
        sc = SCENARIOS[name]
        ft = FakeTime()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = create_app(cfg, name, loop=False, seed=3, clock=AppClock(monotonic=ft.mono, wall=ft.wall),
                             db_path=Path(tmp) / "m.db", engine="ai")
            c, rt = TestClient(app), app.state.runtime
            c.post("/time/sync", json={"app_time": APP_START, "timezone": "Asia/Seoul"})
            c.post("/session/start")
            errors, states = 0, []
            for _ in range(int(sc.total_sec / DT) + 20):
                ft.advance(DT)
                try:
                    rt.tick()
                except Exception:  # noqa: BLE001
                    errors += 1
                cur = rt.engine.latest
                if cur:
                    states.append((cur["seat_state"].value, cur["head_state"].value, cur["tilt_state"].value))
            b = c.get("/baseline").json()
            cur = c.get("/current").json()
            ok(f"{name}: 끝까지 실행(오류 {errors}건), 기준 자세", errors == 0 and b["state"] == "ready", b["state"])
            ok(f"{name}: 상태 값이 약속된 이름", all(s[0] in VALID["seat"] and s[1] in VALID["head"]
                                              and s[2] in VALID["tilt"] for s in states), len(states))
            ok(f"{name}: 신뢰도·대기 상태 전달", set(cur["confidence"]) == {"seat", "head", "tilt"}
               and isinstance(cur["pending"], list), cur["confidence"])
            rep = c.get("/reports/daily", params={"date": "2026-10-15"}).status_code
            seg = c.get("/seating/segments").status_code
            ok(f"{name}: 리포트·착석 구간 API", rep == 200 and seg == 200, (rep, seg))

            want = step_labels(sc.steps[-1])
            got = (cur["seat_state"], cur["head_state"], cur["tilt_state"])
            exp = (want["seat"], want["head"], want["tilt"])
            info.append(f"  {name:<13} 정답 {exp}  AI {got}  {'일치' if got == exp else '다름'}")
            c.post("/session/stop")
            rt.recorder.close(rt.clock.now())

    print("\n참고 — 시나리오 마지막 구간 정답 vs AI 판정 (통과 기준 아님)")
    print("\n".join(info))
    bad = [r for r in ok.rows if not r[0]]
    print(f"\n{len(ok.rows) - len(bad)}/{len(ok.rows)} 통과")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
