from django.utils import timezone

from plane.db.models import User
from plane.license.models import (
    Instance,
    InstanceAdmin,
    SSOAuditEvent,
    SSOIdentity,
    SSOProvider,
)


SSOAuditEvent.objects.all().delete()
SSOIdentity.objects.all().delete()
SSOProvider.objects.all().delete()
InstanceAdmin.objects.all().delete()
Instance.objects.all().delete()
User.objects.filter(
    email__in=["recovery.e2e@example.com", "sso.e2e@example.com"]
).delete()

instance = Instance.objects.create(
    instance_name="Plane SSO E2E",
    instance_id="plane-sso-e2e",
    current_version="e2e",
    last_checked_at=timezone.now(),
    is_setup_done=True,
)
admin = User.objects.create(
    email="recovery.e2e@example.com", username="recovery-e2e", is_email_verified=True
)
admin.set_password("PlaneE2E!Recovery123")
admin.save()
InstanceAdmin.objects.create(instance=instance, user=admin, role=20, is_verified=True)

provider = SSOProvider(
    instance=instance,
    name="Local OIDC",
    slug="local-oidc",
    issuer_url="https://127.0.0.1:9443",
    client_id="plane-e2e",
    scopes=["openid", "email", "profile"],
    allowed_email_domains=["example.com"],
    allowed_groups=["plane-users"],
    jit_provisioning_enabled=True,
    allow_verified_email_auto_link=False,
    is_enabled=True,
    is_enforced=False,
    configuration_tested_at=timezone.now(),
)
provider.set_client_secret("e2e-secret")
provider.save()
print(f"Seeded provider {provider.id}")
