"""Transactional writes through Plane models, retaining original Jira identities."""

import html
from django.db import transaction
from django.utils.dateparse import parse_date, parse_datetime
from plane.db.models import (
    Cycle,
    CycleIssue,
    Issue,
    IssueAssignee,
    IssueComment,
    IssueDescriptionVersion,
    IssueLabel,
    IssueLink,
    IssueRelation,
    IssueVersion,
    JiraItem,
    Label,
    ProjectMember,
    State,
    WorkspaceMember,
)
from plane.utils.confluence.client import ConfluenceError
from plane.utils.confluence.jobs import require_active_run
from plane.utils.jira.content import choose_sprint, convert_document, rendered_html


def base_values(run):
    return {
        "project": run.source.project,
        "workspace": run.source.project.workspace,
        "created_by": run.initiated_by,
        "updated_by": run.initiated_by,
    }


def mapped_user(source, account):
    if not account:
        return None
    active_workspace_ids = WorkspaceMember.objects.filter(workspace=source.project.workspace, is_active=True).values(
        "member_id"
    )
    members = ProjectMember.objects.filter(
        project=source.project, member_id__in=active_workspace_ids, is_active=True, member__is_active=True
    )
    explicit = source.user_mapping.get(account.get("accountId", ""))
    if explicit:
        match = members.filter(member_id=explicit).first()
    elif account.get("emailAddress"):
        match = members.filter(member__email__iexact=account["emailAddress"]).first()
    else:
        match = None
    return match.member if match else None


def writable(destination, source):
    if (
        destination.deleted_at
        or destination.project_id != source.project_id
        or getattr(destination, "archived_at", None)
    ):
        raise ConfluenceError(
            "destination_not_writable",
            "The imported destination was removed, moved or archived. Restore it before syncing.",
        )


def prepare_issue(run, item):
    with transaction.atomic():
        require_active_run(run)
        item = JiraItem.objects.select_for_update(of=("self",)).select_related("issue").get(pk=item.pk)
        if item.issue_id:
            writable(item.issue, run.source)
        else:
            item.issue = Issue.objects.create(
                **base_values(run),
                name=item.remote["fields"].get("summary", item.title)[:255],
                external_source="jira",
                external_id=f"{run.source.site_url}:{item.remote_id}",
            )
            item.save(update_fields=["issue", "updated_at"])
        return item.issue


def save_sprint(run, item):
    remote = item.remote
    values = {
        "name": remote.get("name", item.title)[:255],
        "description": remote.get("goal", ""),
        "start_date": parse_datetime(remote["startDate"]) if remote.get("startDate") else None,
        "end_date": parse_datetime(remote["endDate"]) if remote.get("endDate") else None,
        "external_source": "jira",
        "external_id": f"{run.source.site_url}:{item.remote_id}",
    }
    with transaction.atomic():
        require_active_run(run)
        current = JiraItem.objects.select_for_update(of=("self",)).select_related("cycle").get(pk=item.pk)
        if current.cycle_id:
            cycle = Cycle.objects.select_for_update().get(pk=current.cycle_id)
            writable(cycle, run.source)
            for key, value in values.items():
                setattr(cycle, key, value)
            cycle.version += 1
            cycle.updated_by = run.initiated_by
            cycle.save()
        else:
            cycle = Cycle.objects.create(**base_values(run), **values, owned_by=run.initiated_by)
            current.cycle = cycle
            current.save(update_fields=["cycle", "updated_at"])
        item.cycle = cycle
        # Imported cycles must be reachable from the project's navigation.
        type(run.source.project).objects.filter(pk=run.source.project_id).update(cycle_view=True)


def save_issue(run, item, attachments):
    source = run.source
    fields = item.remote["fields"]
    value = rendered_html(item.remote, source, attachments)
    remote_link = f"{source.site_url}/browse/{item.remote['key']}"
    # Preserve the source identifier and reporter when Jira does not expose their email.
    reporter = fields.get("reporter") or {}
    if reporter:
        value += f"<p>Jira reporter: {html.escape(reporter.get('displayName', 'Unknown'))}</p>"
    value += f'<p><a href="{html.escape(remote_link, quote=True)}">{html.escape(item.remote["key"])}</a></p>'
    assignee = mapped_user(source, fields.get("assignee"))
    warning = ""
    if fields.get("assignee") and not assignee:
        user = fields["assignee"]
        warning = (
            f"Assignee {user.get('displayName', 'Unknown')} ({user.get('accountId', '')}) could not be matched; "
            "choose a project member in user mappings and sync again."
        )
    with transaction.atomic():
        issue = Issue.objects.select_for_update().get(pk=item.issue_id)
        writable(issue, source)
        content_html, content_json, binary = convert_document(value, issue.description_binary)
        require_active_run(run)
        if item.imported_revision:
            IssueDescriptionVersion.objects.create(
                **base_values(run),
                issue=issue,
                owned_by=run.initiated_by,
                description_html=issue.description_html,
                description_json=issue.description_json,
                description_binary=issue.description_binary,
            )
            if not IssueVersion.log_issue_version(issue, run.initiated_by):
                raise ConfluenceError(
                    "version_failed", "Plane could not retain the work item version. Retry this item."
                )
        status = fields.get("status") or {}
        group = {"new": "unstarted", "indeterminate": "started", "done": "completed"}.get(
            (status.get("statusCategory") or {}).get("key"), "backlog"
        )
        state, _ = State.objects.get_or_create(
            project=source.project,
            name=status.get("name", "Todo")[:255],
            defaults={
                **base_values(run),
                "group": group,
                "color": "#60646C",
                "external_source": "jira",
                "external_id": str(status.get("id", "")),
            },
        )
        issue.name = fields.get("summary", item.title)[:255]
        issue.state = state
        priority = (fields.get("priority") or {}).get("name", "").lower()
        issue.priority = {
            "highest": "urgent",
            "blocker": "urgent",
            "critical": "urgent",
            "high": "high",
            "medium": "medium",
            "low": "low",
            "lowest": "low",
        }.get(priority, "none")
        issue.target_date = parse_date(fields["duedate"]) if fields.get("duedate") else None
        issue.description_html, issue.description_json, issue.description_binary = content_html, content_json, binary
        issue.updated_by = run.initiated_by
        issue.save()
        IssueLink.objects.get_or_create(
            project=source.project,
            issue=issue,
            url=remote_link,
            defaults={**base_values(run), "title": item.remote["key"]},
        )
        label_ids = []
        for name in fields.get("labels") or []:
            label, _ = Label.objects.get_or_create(
                project=source.project,
                name=name[:255],
                defaults={**base_values(run), "color": "#60646C", "external_source": "jira", "external_id": name[:255]},
            )
            label_ids.append(label.pk)
            IssueLabel.objects.get_or_create(issue=issue, label=label, defaults=base_values(run))
        IssueLabel.objects.filter(issue=issue).exclude(label_id__in=label_ids).delete()
        IssueAssignee.objects.filter(issue=issue).exclude(assignee=assignee).delete()
        if assignee:
            IssueAssignee.objects.get_or_create(issue=issue, assignee=assignee, defaults=base_values(run))
    return warning


def save_comment(run, item, attachments):
    source = run.source
    parent = source.items.get(kind="issue", remote_id=item.remote["issueId"])
    if not parent.issue_id:
        raise ConfluenceError("issue_dependency_failed", "Import the comment's work item first.")
    author = item.remote.get("author") or {}
    actor = mapped_user(source, author)
    value = rendered_html(item.remote, source, attachments, comment=True)
    # Do not impersonate the importer as an unmapped Jira author.
    if not actor:
        value = f"<p>Jira author: {html.escape(author.get('displayName', 'Unknown'))}</p>" + value
    content_html, content_json, _ = convert_document(value)
    with transaction.atomic():
        require_active_run(run)
        writable(parent.issue, source)
        current = JiraItem.objects.select_for_update(of=("self",)).select_related("comment").get(pk=item.pk)
        values = {
            "comment_html": content_html,
            "comment_json": content_json,
            "actor": actor,
            "external_source": "jira",
            "external_id": f"{source.site_url}:{item.remote_id}",
            "edited_at": parse_datetime(item.remote["updated"]) if item.remote.get("updated") else None,
        }
        if current.comment_id:
            writable(current.comment, source)
            for key, value in values.items():
                setattr(current.comment, key, value)
            current.comment.updated_by = run.initiated_by
            current.comment.save()
        else:
            current.comment = IssueComment.objects.create(**base_values(run), **values, issue=parent.issue)
            if item.remote.get("created"):
                IssueComment.objects.filter(pk=current.comment_id).update(
                    created_at=parse_datetime(item.remote["created"])
                )
            current.save(update_fields=["comment", "updated_at"])
        item.comment = current.comment
    return (
        ""
        if actor
        else f"Original author retained as attribution; unmapped Jira account {author.get('accountId', 'unknown')}."
    )


def save_relationships(run, item):
    """Resolve parent/subtask, issue links, and current sprint after destinations exist."""
    source = run.source
    fields = item.remote["fields"]
    with transaction.atomic():
        issue = Issue.objects.select_for_update().get(pk=item.issue_id)
        writable(issue, source)
        require_active_run(run)
        parent_id = str((fields.get("parent") or {}).get("id", ""))
        parent = source.items.filter(kind="issue", remote_id=parent_id).first() if parent_id else None
        if parent_id and (not parent or not parent.issue_id):
            raise ConfluenceError(
                "parent_not_imported", "This parent is outside the selected Jira project or failed to import."
            )
        if parent:
            writable(parent.issue, source)
            visited = {issue.id}
            ancestor = parent.issue
            while ancestor:
                if ancestor.id in visited:
                    raise ConfluenceError("invalid_hierarchy", "Jira returned a cyclic parent/subtask hierarchy.")
                visited.add(ancestor.id)
                ancestor = ancestor.parent
        issue.parent = parent.issue if parent else None
        issue.save(update_fields=["parent", "updated_at"])
        sprint = choose_sprint(item.remote.get("sprints", []))
        cycle_item = source.items.filter(kind="sprint", remote_id=str(sprint["id"])).first() if sprint else None
        if sprint and (not cycle_item or not cycle_item.cycle_id or cycle_item.imported_revision == ""):
            raise ConfluenceError(
                "sprint_not_imported", "The work item's sprint could not be imported. Retry the sprint and work item."
            )
        if cycle_item and run.results.filter(item=cycle_item, status="failed").exists():
            raise ConfluenceError("sprint_not_imported", "Retry the failed sprint before updating its work items.")
        CycleIssue.objects.filter(issue=issue).exclude(cycle_id=cycle_item.cycle_id if cycle_item else None).delete()
        if cycle_item:
            writable(cycle_item.cycle, source)
            CycleIssue.objects.get_or_create(issue=issue, cycle=cycle_item.cycle, defaults=base_values(run))
        for link in fields.get("issuelinks", []):
            target = link.get("inwardIssue") or link.get("outwardIssue") or {}
            other = source.items.filter(kind="issue", remote_id=str(target.get("id", ""))).first()
            if not other or not other.issue_id or other.issue_id == issue.id:
                continue
            name = (link.get("type") or {}).get("name", "").lower()
            kind = "duplicate" if "duplicate" in name else "blocked_by" if "block" in name else "relates_to"
            first, second = issue, other.issue
            if kind == "blocked_by" and link.get("outwardIssue"):
                first, second = second, first
            IssueRelation.objects.update_or_create(
                issue=first, related_issue=second, defaults={**base_values(run), "relation_type": kind}
            )
