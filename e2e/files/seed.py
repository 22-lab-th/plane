"""Seed the base rows for the Files tab E2E run (workspace, project, members).

The folder tree and the files are created through the **API** afterwards, by
`e2e/files/seed_content.py`, so the fixture the spec walks is one the product
itself produced rather than a hand-written store.
"""

from django.utils import timezone

from plane.db.models import Profile, Project, ProjectMember, User, Workspace, WorkspaceMember
from plane.license.models import Instance

OWNER_EMAIL = "files.e2e@example.com"
OWNER_PASSWORD = "PlaneE2E!Files123"
GUEST_EMAIL = "files.guest.e2e@example.com"
GUEST_PASSWORD = "PlaneE2E!Guest123"

Instance.objects.all().delete()
User.objects.filter(email__in=[OWNER_EMAIL, GUEST_EMAIL]).delete()

Instance.objects.create(
    instance_name="Plane Files E2E",
    instance_id="plane-files-e2e",
    current_version="e2e",
    last_checked_at=timezone.now(),
    is_setup_done=True,
)

owner = User.objects.create(email=OWNER_EMAIL, username="files-e2e", is_email_verified=True)
owner.set_password(OWNER_PASSWORD)
owner.save()

guest = User.objects.create(email=GUEST_EMAIL, username="files-guest-e2e", is_email_verified=True)
guest.set_password(GUEST_PASSWORD)
guest.save()

# The web app sends a signed-in user whose profile is not onboarded to /onboarding/,
# so both members are marked onboarded here (that is what the app's own flow sets).
for member in (owner, guest):
    profile, _ = Profile.objects.get_or_create(user=member)
    profile.is_onboarded = True
    profile.save()

workspace = Workspace.objects.create(name="Files E2E", slug="files-e2e", owner=owner)
WorkspaceMember.objects.create(workspace=workspace, member=owner, role=20, is_active=True)
WorkspaceMember.objects.create(workspace=workspace, member=guest, role=5, is_active=True)

project = Project.objects.create(name="Files E2E Project", identifier="FE2E", workspace=workspace)
ProjectMember.objects.create(project=project, member=owner, workspace=workspace, role=20, is_active=True)
ProjectMember.objects.create(project=project, member=guest, workspace=workspace, role=5, is_active=True)

print(f"E2E_WORKSPACE_SLUG={workspace.slug}")
print(f"E2E_PROJECT_ID={project.id}")
