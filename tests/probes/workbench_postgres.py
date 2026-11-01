"""Fixed integration probe, executed only in the API Linux container via Docker SDK."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import time
import uuid

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from rd_platform.service.run_store import RunStore, StoreConflict, runs


def main():
    url = make_url(os.environ["RD_DATABASE_URL"])
    admin = create_engine(url)
    schema = "wb_probe_" + uuid.uuid4().hex
    checks = []
    store = None
    try:
        with admin.begin() as db:
            version = db.scalar(text("SHOW server_version"))
            assert version.startswith("17."), version
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        isolated = url.update_query_dict({"options": f"-csearch_path={schema} -cstatement_timeout=5000"})
        store = RunStore(isolated)
        project_id = uuid.uuid4().hex
        store.save_project({"id": project_id, "name": "isolated queue probe", "context": {}, "tasks": []})
        for _ in range(2):
            store.create_run(project_id, "codex", "probe")
        worker_ids = [uuid.uuid4().hex for _ in range(3)]
        for worker_id in worker_ids:
            store.heartbeat(worker_id, ["codex"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(store.claim, worker_ids[:2]))
        assert len({run["id"] for run in claimed}) == 2
        checks.append("concurrent_workers_claim_distinct_runs")
        assert store.claim(worker_ids[0])["id"] == claimed[0]["id"]
        checks.append("lost_claim_response_is_idempotent")
        third = store.create_run(project_id, "codex", "locked probe")
        with store.engine.begin() as lock:
            lock.execute(select(runs.c.id).where(runs.c.id == third["id"]).with_for_update()).first()
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(store.claim, worker_ids[2]).result(timeout=3) is None
        assert store.claim(worker_ids[2])["id"] == third["id"]
        checks.append("locked_row_skipped_without_blocking")
        with store.transaction() as db:
            db.execute(runs.update().where(runs.c.id == claimed[0]["id"]).values(lease_until=time.time() - 1))
        store.heartbeat(worker_ids[0], ["codex"])
        assert store.get_run(claimed[0]["id"])["status"] == "INTERRUPTED"
        try:
            store.finish(claimed[0]["id"], worker_ids[0], 1, {"status": "FAILED"})
        except StoreConflict:
            pass
        else:
            raise AssertionError("expired generation was accepted")
        checks.append("expired_generation_cannot_publish")
        summary = {"status": "FAILED", "error": "intentional isolated probe"}
        store.finish(third["id"], worker_ids[2], 1, summary)
        store.finish(third["id"], worker_ids[2], 1, summary)
        checks.append("completion_retransmission_is_idempotent")
        store.close()
        store = RunStore(isolated)
        assert store.get_run(third["id"])["status"] == "FAILED"
        checks.append("records_survive_store_restart")
        print(json.dumps({"status": "PASS", "postgres_version": version, "checks": checks}))
    finally:
        if store:
            store.close()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


if __name__ == "__main__":
    main()
