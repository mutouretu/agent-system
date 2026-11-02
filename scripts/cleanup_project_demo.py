"""Back up and remove only persistent projects created by project_demo.py.

Uses Docker SDK to run fixed SQL through psql in the local Compose database.
The command is dry-run by default; --apply performs the guarded deletion.
"""

import argparse
import json
import os
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[1]
MATCH = "payload->>'name' = 'Data to statistics' AND payload->'context'->>'source' = 'project_demo'"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete matching projects after backup")
    parser.add_argument("--expected", type=int, required=True, help="exact number of projects expected")
    args = parser.parse_args()
    if args.expected < 0:
        parser.error("--expected must be non-negative")

    evidence = ROOT / "sandbox-validation" / f"demo-project-cleanup-{uuid.uuid4().hex}"
    evidence.mkdir(parents=True)
    docker_config = evidence / "docker-config"
    docker_config.mkdir()
    os.environ["DOCKER_CONFIG"] = str(docker_config)

    import docker

    client = docker.from_env(timeout=30)
    try:
        containers = [container for container in client.containers.list() if
                      container.labels.get("com.docker.compose.project") == "rd-platform-service" and
                      container.labels.get("com.docker.compose.service") == "db"]
        if len(containers) != 1:
            raise RuntimeError("expected exactly one local rd-platform-service database container")
        db = containers[0]

        def sql(statement):
            result = db.exec_run(["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                                  "-U", "rd", "-d", "rd_platform", "-c", statement])
            output = result.output.decode("utf-8", errors="replace").strip()
            if result.exit_code:
                raise RuntimeError(f"database command failed: {output}")
            return output

        backup_sql = ("SELECT coalesce(json_agg(json_build_object('id', id, 'payload', payload, "
                      "'created_at', created_at) ORDER BY created_at), '[]'::json) "
                      f"FROM workbench_projects WHERE {MATCH}")
        rows = json.loads(sql(backup_sql))
        if len(rows) != args.expected or len({row["id"] for row in rows}) != len(rows):
            raise RuntimeError(f"matching project count changed: expected {args.expected}, found {len(rows)}")
        linked_sql = ("SELECT count(*) FROM workbench_runs WHERE project_id IN "
                      f"(SELECT id FROM workbench_projects WHERE {MATCH})")
        linked_exists_sql = ("SELECT 1 FROM workbench_runs WHERE project_id IN "
                             f"(SELECT id FROM workbench_projects WHERE {MATCH})")
        if int(sql(linked_sql)):
            raise RuntimeError("matching projects have linked development runs; refusing deletion")
        backup = evidence / "projects-backup.json"
        backup.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        backup.chmod(0o600)

        deleted = 0
        if args.apply and rows:
            guarded_delete = (
                "BEGIN; LOCK TABLE workbench_projects IN SHARE ROW EXCLUSIVE MODE; "
                "DO $$ DECLARE found_count integer; BEGIN "
                f"SELECT count(*) INTO found_count FROM workbench_projects WHERE {MATCH}; "
                f"IF found_count <> {args.expected} THEN RAISE EXCEPTION 'project count changed'; END IF; "
                f"IF EXISTS ({linked_exists_sql}) THEN "
                "RAISE EXCEPTION 'matching project has a development run'; END IF; "
                f"DELETE FROM workbench_projects WHERE {MATCH}; "
                "END; $$; COMMIT;"
            )
            sql(guarded_delete)
            remaining = int(sql(f"SELECT count(*) FROM workbench_projects WHERE {MATCH}"))
            if remaining:
                raise RuntimeError(f"deletion incomplete: {remaining} matching projects remain")
            deleted = len(rows)

        summary = {"status": "PASS", "mode": "apply" if args.apply else "dry-run",
                   "matched": len(rows), "deleted": deleted, "backup": str(backup)}
        (evidence / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(summary, ensure_ascii=False))
    finally:
        client.close()


if __name__ == "__main__":
    main()
