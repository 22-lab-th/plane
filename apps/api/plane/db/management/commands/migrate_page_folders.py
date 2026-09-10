"""Apply or restore a recorded, project-scoped Page folder mapping."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from plane.db.models import Page, Project, ProjectPage


MIGRATION_SOURCE = "22lab:page-folder-migration"


class Command(BaseCommand):
    help = "Apply or restore an explicit Page folder mapping without rewriting Page content"

    def add_arguments(self, parser):
        parser.add_argument("--project", required=True)
        parser.add_argument("--mapping-file", required=True)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--restore", action="store_true")

    def handle(self, *args, **options):
        mapping_path = Path(options["mapping_file"]).resolve()
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Cannot read mapping file: {exc}") from exc

        project_id = str(options["project"])
        if mapping.get("project_id") != project_id:
            raise CommandError("Mapping project_id does not match --project")

        project = Project.objects.select_related("workspace").filter(pk=project_id).first()
        if project is None:
            raise CommandError("Project not found")

        assignments = mapping.get("assignments") or []
        expected_ids = {row["page_id"] for row in assignments}
        pages = {
            str(page.id): page
            for page in Page.objects.filter(
                id__in=expected_ids,
                workspace=project.workspace,
                project_pages__project=project,
                project_pages__deleted_at__isnull=True,
            ).distinct()
        }
        missing_ids = sorted(expected_ids - set(pages))
        if missing_ids:
            raise CommandError(f"Mapping references missing project pages: {', '.join(missing_ids)}")

        plan = {
            "version": mapping.get("version"),
            "project_id": project_id,
            "mode": "restore" if options["restore"] else "apply" if options["apply"] else "plan",
            "pages": len(assignments),
            "folders": len(mapping.get("folders") or []),
        }
        if not options["apply"] and not options["restore"]:
            self.stdout.write(json.dumps(plan, sort_keys=True))
            return

        with transaction.atomic():
            if options["restore"]:
                Page.objects.filter(id__in=expected_ids).update(parent=None, sort_order=Page.DEFAULT_SORT_ORDER)
                Page.objects.filter(
                    workspace=project.workspace,
                    external_source=MIGRATION_SOURCE,
                    external_id__startswith=f"{mapping['version']}:",
                ).update(archived_at=timezone.now().date())
            else:
                owner = next(iter(pages.values())).owned_by
                folders = {}
                for folder_spec in mapping.get("folders") or []:
                    external_id = f"{mapping['version']}:{folder_spec['key']}"
                    parent = folders.get(folder_spec.get("parent"))
                    folder, _ = Page.objects.get_or_create(
                        workspace=project.workspace,
                        external_source=MIGRATION_SOURCE,
                        external_id=external_id,
                        defaults={
                            "name": folder_spec["name"],
                            "owned_by": owner,
                            "access": Page.PUBLIC_ACCESS,
                            "node_type": Page.FOLDER_NODE,
                            "parent": parent,
                            "sort_order": folder_spec.get("sort_order", Page.DEFAULT_SORT_ORDER),
                        },
                    )
                    folder.name = folder_spec["name"]
                    folder.node_type = Page.FOLDER_NODE
                    folder.access = Page.PUBLIC_ACCESS
                    folder.parent = parent
                    folder.sort_order = folder_spec.get("sort_order", Page.DEFAULT_SORT_ORDER)
                    folder.archived_at = None
                    folder.save()
                    ProjectPage.objects.get_or_create(
                        project=project,
                        page=folder,
                        defaults={"workspace": project.workspace, "created_by": owner},
                    )
                    folders[folder_spec["key"]] = folder

                for row in assignments:
                    page = pages[row["page_id"]]
                    page.parent = folders[row["folder"]]
                    page.sort_order = row.get("sort_order", Page.DEFAULT_SORT_ORDER)
                    page.save(update_fields=["parent", "sort_order", "updated_at"])

        self.stdout.write(json.dumps(plan, sort_keys=True))
