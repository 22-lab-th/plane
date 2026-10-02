from plane.utils.confluence.jobs import serialize_run as serialize_import_run


def serialize_run(run):
    return serialize_import_run(
        run,
        source_summary={
            "remote_project_id": run.source.remote_project_id,
            "project_key": run.source.project_key,
            "project_name": run.source.project_name,
        },
    )
