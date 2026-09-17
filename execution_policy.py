"""작업 시작 시 한 번 선택하고 재개 시 유지하는 실행 정책. LLM 호출 없음."""
import re

RISK = re.compile(r"인증|로그인|권한|결제|데이터\s*(삭제|손실)|마이그레이션|장애|보안|authentication|authorization|login|payment|migration|outage|security", re.I)


def select_profile(manifest, config, previous=None, escalate=False):
    profiles = config.get("execution_profiles")
    if not profiles:
        effort = manifest.get("worker_effort", "auto")
        return (manifest.get("worker_model") or (previous or {}).get("model") or config["worker"]["model"],
                effort if effort != "auto" else (previous or {}).get("effort") or config["worker"]["default_effort"])
    rank = {"simple": 0, "medium": 1, "complex": 2,
            "high_risk": 2, "batch_or_repo_wide": 2}[manifest["task_class"]]
    if RISK.search(manifest["objective"]):
        rank = max(rank, 2)
    if previous:
        # 이어지는 작업은 내리지 않는다. 승격은 추론/검증 실패를 확인한 호출자가 요청한다.
        old_rank = next((i for i, p in enumerate(profiles)
                         if p["effort"] == previous.get("effort")), 1)
        rank = max(rank, min(old_rank + int(escalate), len(profiles) - 1))
    profile = profiles[rank]
    model = manifest.get("worker_model") or (
        previous.get("model") if previous and rank <= old_rank and not escalate else None) or profile["model"]
    effort = manifest.get("worker_effort", "auto")
    if effort == "auto":
        effort = profile["effort"]
    return model, effort
