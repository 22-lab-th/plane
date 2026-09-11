<br /><br />

<p align="center">
<a href="https://plane.so">
  <img src="https://media.docs.plane.so/logo/plane_github_readme.png" alt="Plane Logo" width="400">
</a>
</p>
<p align="center"><b>Modern project management for all teams</b></p>

<p align="center">
    <a href="https://plane.so/"><b>Website</b></a> •
    <a href="https://forum.plane.so"><b>Forum</b></a> •
    <a href="https://x.com/planepowers"><b>X</b></a> •
    <a href="https://docs.plane.so/"><b>Documentation</b></a>
</p>

<p>
    <a href="https://app.plane.so/#gh-light-mode-only" target="_blank">
      <img
        src="https://media.docs.plane.so/GitHub-readme/github-top.webp"
        alt="Plane Screens"
        width="100%"
      />
    </a>
</p>

Meet [Plane](https://plane.so/), an open-source project management tool to track issues, run ~sprints~ cycles, and manage product roadmaps without the chaos of managing the tool itself. 🧘‍♀️

> Plane is evolving every day. Your suggestions, ideas, and reported bugs help us immensely. Do not hesitate to join in the conversation on [Forum](https://forum.plane.so) or raise a GitHub issue. We read everything and respond to most.

> [!IMPORTANT]
> This repository is the **22lab-maintained Plane distribution**. It is based on upstream Plane and adds document organization, shared bookmarks, and optional OIDC SSO. The `preview` branch is the integration and delivery branch for these additions.

## 22lab additions

The following behavior is different from the upstream Plane `preview` branch:

- **Nested Page folders** — create, rename, reorder, archive, restore, and move Pages through a folder hierarchy. Drag-and-drop and dialog-based move actions are both available.
- **Page migration tools** — import individual Markdown files or Markdown bundles with images and move documents between projects without rewriting their content.
- **Workspace Bookmarks** — shared, grouped bookmarks with URL metadata autofill, group filtering, search, and a focused split-view inspector.
- **Optional OpenID Connect SSO** — configure one instance-wide OIDC provider in God Mode, test discovery/readiness, link or provision identities according to policy, and roll out in Disabled, Optional, or Enforced mode.
- **SSO recovery and audit controls** — break-glass instance-admin access, readiness checks, lifecycle safeguards, correlation IDs, and audit events that exclude tokens, raw claims, and client secrets.
- **Local deployment hardening** — CSRF-safe instance registration and separate internal/public MinIO endpoints so browser uploads work when the API and object store use container networking.

Design and operational detail is available in:

- [Page folders specification](./docs/specs/page-folders-22lab.md)
- [Workspace Bookmarks design QA](./design-qa.md)
- [OIDC operations runbook](./docs/bmad/sso-operations.md)
- [OIDC delivery report](./docs/auto/delivery-report.md)

## 🚀 Installation

Choose the path that matches the intended environment:

- **Plane Cloud**
  Sign up for a free account on [Plane Cloud](https://app.plane.so)—it's the fastest way to get up and running without worrying about infrastructure.

- **Upstream Plane self-hosting**
  Follow Plane's [self-hosting overview](https://developers.plane.so/self-hosting/overview) if the 22lab additions are not required.

- **22lab distribution**
  Clone this repository and follow either [Local development with Podman or Docker](#local-development-with-podman-or-docker) or [Customer deployment](#customer-deployment). Build the Web, Admin, and API components from the same Git revision.

| Installation methods | Docs link                                                                                                                                                                               |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Docker               | [![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=for-the-badge&logo=docker&logoColor=white)](https://developers.plane.so/self-hosting/methods/docker-compose)         |
| Kubernetes           | [![Kubernetes](https://img.shields.io/badge/kubernetes-%23326ce5.svg?style=for-the-badge&logo=kubernetes&logoColor=white)](https://developers.plane.so/self-hosting/methods/kubernetes) |

`Instance admins` can configure instance settings with [God mode](https://developers.plane.so/self-hosting/govern/instance-admin).

## Customer deployment

Use Plane's supported [Docker](https://developers.plane.so/self-hosting/methods/docker-compose) or [Kubernetes](https://developers.plane.so/self-hosting/methods/kubernetes) deployment guidance as the infrastructure baseline, but build images from this repository's `preview` branch so the Admin, Web, API, workers, and migrations remain on the same revision.

Start from a versioned checkout and record the deployed commit:

```bash
git clone https://github.com/22-lab-th/plane.git
cd plane
git checkout preview
git pull --ff-only origin preview
git rev-parse HEAD
```

Before deploying:

1. Back up PostgreSQL and object storage, and record the currently deployed image tags or commit.
2. Copy the environment templates and replace every sample password, access key, and application secret. Do not commit any generated `.env` file.
3. Set `APP_BASE_URL`, `ADMIN_BASE_URL`, `SPACE_BASE_URL`, `LIVE_BASE_URL`, `WEB_URL`, and `CORS_ALLOWED_ORIGINS` to the final HTTPS origins. Configure trusted reverse proxies and TLS before exposing the instance.
4. Configure database, Valkey/Redis, RabbitMQ, email, and object storage for the target environment.
5. If S3/MinIO has different container and browser addresses, set `AWS_S3_ENDPOINT_URL` to the API-accessible address and `MINIO_PUBLIC_ENDPOINT_URL` to the browser-accessible HTTPS address.
6. Apply all migrations before enabling the new frontend. The folder, bookmark, and OIDC migrations are additive, but a database backup is still required for rollback.
7. Create and verify at least one instance administrator in God Mode, then complete the [smoke-test checklist](#smoke-test-checklist).

Do not use `docker-compose-local.yml`, development servers, sample credentials, or plain HTTP for a production deployment.

The repository implementation and automated checks are complete, but deployment acceptance is environment-specific. A release is not accepted until normal login is smoke-tested with OIDC disabled in the target environment and, when applicable, the customer OIDC tenant is tested end to end. See the [OIDC delivery report](./docs/auto/delivery-report.md) for the latest evidence and remaining acceptance gates.

### Authentication modes

OIDC is a deployment capability and is **disabled by default**. Existing password, magic-code, and configured social-login flows continue to work when OIDC is off.

| Intended mode     | Deployment setting  | God Mode provider state   | User experience                                                              |
| ----------------- | ------------------- | ------------------------- | ---------------------------------------------------------------------------- |
| Password/non-OIDC | `ENABLE_OIDC_SSO=0` | Unavailable and read-only | Existing login methods only                                                  |
| OIDC available    | `ENABLE_OIDC_SSO=1` | Enabled, not enforced     | OIDC and existing login methods                                              |
| OIDC required     | `ENABLE_OIDC_SSO=1` | Enabled and enforced      | OIDC for normal users; approved break-glass admin recovery remains available |

For 22lab's current non-OIDC environment, leave `ENABLE_OIDC_SSO=0`. A customer who needs OIDC should set it to `1`, restart the API, and then configure the provider after deployment:

1. Set `SSO_BREAK_GLASS_ADMIN_EMAILS` to a comma-separated list containing at least one active, verified instance administrator. Protect that account in the deployment's secret manager.
2. Register the exact callback URI `<APP_BASE_URL>/auth/sso/callback/` at the identity provider.
3. Sign in to God Mode and open **Authentication → SSO / OpenID Connect**.
4. Save the issuer, client ID, client secret, scopes, and policy with **Enable provider** and **Enforce SSO** turned off.
5. Run **Test connection**, then validate a real user login while the provider is Optional.
6. Test break-glass access, logout, IdP outage, secret rotation, and rollback before enabling Enforced mode.

Never store a customer OIDC secret in this repository, documentation, screenshots, tickets, or logs. See the [OIDC operations runbook](./docs/bmad/sso-operations.md) for private IdP allowlists, private CA configuration, recovery, audit, rotation, and rollback procedures.

> [!NOTE]
> Repository tests cover a local HTTPS mock IdP and representative Entra ID, Okta, and Keycloak metadata. Each customer must still validate the authorization-code flow against their real tenant and reverse proxy before production enforcement.

## 🌟 Features

- **Work Items**
  Efficiently create and manage tasks with a robust rich text editor that supports file uploads. Enhance organization and tracking by adding sub-properties and referencing related issues.

- **Cycles**
  Maintain your team’s momentum with Cycles. Track progress effortlessly using burn-down charts and other insightful tools.

- **Modules**
  Simplify complex projects by dividing them into smaller, manageable modules.

- **Views**
  Customize your workflow by creating filters to display only the most relevant issues. Save and share these views with ease.

- **Pages**
  Capture and organize ideas using Plane Pages, including nested folders, Markdown import, image bundles, document moves, AI capabilities, and a rich text editor.

- **Workspace Bookmarks**
  Maintain shared references in searchable groups with automatic URL metadata and a focused detail view.

- **Optional OIDC SSO**
  Offer or enforce an instance-wide OIDC provider while retaining a fully supported non-OIDC deployment mode and protected administrative recovery.

- **Analytics**
  Access real-time insights across all your Plane data. Visualize trends, remove blockers, and keep your projects moving forward.

## 🛠️ Local development with Podman or Docker

### Prerequisites

- Git
- Node.js `>=22.22.0`
- Corepack and pnpm `11.3.0` (the exact pnpm version is pinned in `package.json`)
- Podman with `podman compose`, or Docker with Compose v2
- At least 8 GB RAM; more is recommended when building every application locally

### Setup

1. Clone and select the delivery branch:

   ```bash
   git clone https://github.com/22-lab-th/plane.git
   cd plane
   git checkout preview
   chmod +x setup.sh
   ```

2. Generate local environment files and install JavaScript dependencies:

   ```bash
   ./setup.sh
   ```

   `setup.sh` copies every `.env.example` to `.env`, generates the Django `SECRET_KEY`, and runs `pnpm install`. **It overwrites existing local `.env` files**, so back them up before running the script again.

3. Choose either `localhost` or `127.0.0.1` for browser-facing URLs and use it consistently in every app environment file. Mixing the two can cause cookies or CSRF validation to fail.

   For the provided local Compose stack, verify these values in `apps/api/.env` (the example below uses `127.0.0.1`):

   ```dotenv
   CORS_ALLOWED_ORIGINS="http://127.0.0.1:3000,http://127.0.0.1:3001,http://127.0.0.1:3002,http://127.0.0.1:3100"
   AWS_S3_ENDPOINT_URL="http://plane-minio:9000"
   MINIO_PUBLIC_ENDPOINT_URL="http://127.0.0.1:9000"
   USE_MINIO=1
   WEB_URL="http://127.0.0.1:8000"
   APP_BASE_URL="http://127.0.0.1:3000"
   ADMIN_BASE_URL="http://127.0.0.1:3001"
   SPACE_BASE_URL="http://127.0.0.1:3002"
   LIVE_BASE_URL="http://127.0.0.1:3100"
   ENABLE_OIDC_SSO=0
   ```

   Update the browser-facing base URLs in `apps/web/.env`, `apps/admin/.env`, `apps/space/.env`, and `apps/live/.env` to the same hostname. Keep OIDC disabled unless the local test specifically requires it.

4. Start the backend dependencies and API. Use one of these equivalent commands:

   ```bash
   podman compose -f docker-compose-local.yml up --build -d
   ```

   ```bash
   docker compose -f docker-compose-local.yml up --build -d
   ```

5. Start the frontend development servers:

   ```bash
   pnpm dev
   ```

6. Open God Mode, register the first instance administrator, and use the same account to sign in to the Web app:

   | Service            | URL                               |
   | ------------------ | --------------------------------- |
   | Web                | `http://127.0.0.1:3000`           |
   | God Mode / Admin   | `http://127.0.0.1:3001/god-mode/` |
   | Space              | `http://127.0.0.1:3002/spaces/`   |
   | API                | `http://127.0.0.1:8000`           |
   | Live collaboration | `http://127.0.0.1:3100/live/`     |
   | MinIO API          | `http://127.0.0.1:9000`           |
   | MinIO console      | `http://127.0.0.1:9090`           |

If instance registration reports a CSRF error, confirm cookies are enabled, use the same hostname on every URL, refresh God Mode, and submit the form again.

### Useful commands

```bash
# View service status
podman compose -f docker-compose-local.yml ps

# Follow API logs
podman compose -f docker-compose-local.yml logs -f api

# Stop the local stack without deleting volumes
podman compose -f docker-compose-local.yml down

# Run frontend formatting, lint, and type checks
pnpm check

# Run the isolated backend test stack
docker compose -f docker-compose-test.yml up --build --abort-on-container-exit --exit-code-from api-tests

# Run the local HTTPS OIDC browser scenario
pnpm exec playwright install chromium
pnpm test:e2e:sso
```

Replace `podman compose` with `docker compose` in the status/log/stop commands when using Docker. For backend test conventions and subsets, see [Running backend tests](./apps/api/tests/RUNNING_TESTS.md). General contribution guidance remains in [CONTRIBUTING.md](./CONTRIBUTING.md).

## Smoke-test checklist

Complete this checklist after a fresh installation and before handing a deployment to users:

- [ ] Register an instance administrator in God Mode and sign in through the normal Web login.
- [ ] With `ENABLE_OIDC_SSO=0`, confirm normal login, logout, password/magic-code behavior, and invitation onboarding do not depend on an IdP.
- [ ] Create a workspace and project, upload/change a project cover, and confirm the browser can read and write MinIO/S3 objects.
- [ ] Create nested Page folders; move, archive, restore, and open an existing Page; import a Markdown file and a bundle containing images.
- [ ] Move a Page between projects and confirm its content and assets remain intact.
- [ ] Create bookmark groups and bookmarks; verify URL metadata, filtering, search, edit, open, and delete behavior.
- [ ] If OIDC is required, complete Optional-mode login against the customer's real tenant and verify allowed/denied policy cases, logout, audit events, secret rotation, IdP outage, disable, and break-glass recovery.
- [ ] Back up the database/object store and verify the documented application rollback before enabling OIDC enforcement.

## Production safety checklist

- Keep `.env`, database dumps, OIDC secrets, tokens, and access keys outside source control.
- Use strong unique credentials and a secret manager; rotate all values copied from examples.
- Terminate TLS correctly, set final HTTPS origins, restrict `TRUSTED_PROXIES`, and use the smallest possible `OIDC_ALLOWED_IPS` allowlist for private IdPs.
- Never disable OIDC TLS certificate or hostname verification. Use `OIDC_CA_BUNDLE` for an approved private CA.
- Monitor application, worker, database, queue, and object-storage health; retain tested backups and restore procedures.
- Keep a tested non-SSO instance administrator while OIDC is enforced, and regularly review `sso_audit_events`.
- Review [SECURITY.md](./SECURITY.md) before exposing the deployment publicly.

## Upstream maintenance

`upstream` should point to `https://github.com/makeplane/plane.git`, while `origin` points to the 22lab fork. Before syncing a new upstream release, review database migrations, authentication middleware, Page models/APIs, and workspace routes for conflicts with the additions listed above. Re-run backend contract tests, frontend checks/builds, the non-OIDC smoke test, and the OIDC browser scenario after every sync.

## ⚙️ Built with

[![React Router](https://img.shields.io/badge/-React%20Router-CA4245?logo=react-router&style=for-the-badge&logoColor=white)](https://reactrouter.com/)
[![Django](https://img.shields.io/badge/Django-092E20?style=for-the-badge&logo=django&logoColor=green)](https://www.djangoproject.com/)
[![Node JS](https://img.shields.io/badge/node.js-339933?style=for-the-badge&logo=Node.js&logoColor=white)](https://nodejs.org/en)

## 📸 Screenshots

  <p>
    <a href="https://plane.so" target="_blank">
      <img
        src="https://media.docs.plane.so/GitHub-readme/github-work-items.webp"
        alt="Plane Views"
        width="100%"
      />
    </a>
  </p>
  <p>
    <a href="https://plane.so" target="_blank">
      <img
        src="https://media.docs.plane.so/GitHub-readme/github-cycles.webp"
        width="100%"
      />
    </a>
  </p>
  <p>
    <a href="https://plane.so" target="_blank">
      <img
        src="https://media.docs.plane.so/GitHub-readme/github-modules.webp"
        alt="Plane Cycles and Modules"
        width="100%"
      />
    </a>
  </p>
  <p>
    <a href="https://plane.so" target="_blank">
      <img
        src="https://media.docs.plane.so/GitHub-readme/github-views.webp"
        alt="Plane Analytics"
        width="100%"
      />
    </a>
  </p>
  <p>
    <a href="https://plane.so" target="_blank">
      <img
        src="https://media.docs.plane.so/GitHub-readme/github-analytics.webp"
        alt="Plane Pages"
        width="100%"
      />
    </a>
  </p>

## 📝 Documentation

Explore Plane's [product documentation](https://docs.plane.so/) and [developer documentation](https://developers.plane.so/) to learn about features, setup, and usage.

## ❤️ Community

Join the Plane community on [GitHub Discussions](https://github.com/orgs/makeplane/discussions) and our [Forum](https://forum.plane.so). We follow a [Code of conduct](https://github.com/makeplane/plane/blob/master/CODE_OF_CONDUCT.md) in all our community channels.

Feel free to ask questions, report bugs, participate in discussions, share ideas, request features, or showcase your projects. We’d love to hear from you!

## 🛡️ Security

If you discover a security vulnerability in Plane, please report it responsibly instead of opening a public issue. We take all legitimate reports seriously and will investigate them promptly. See [Security policy](https://github.com/makeplane/plane/blob/master/SECURITY.md) for more info.

To disclose any security issues, please email us at security@plane.so.

## 🤝 Contributing

There are many ways you can contribute to Plane:

- Report [bugs](https://github.com/makeplane/plane/issues/new?assignees=srinivaspendem%2Cpushya22&labels=%F0%9F%90%9Bbug&projects=&template=--bug-report.yaml&title=%5Bbug%5D%3A+) or submit [feature requests](https://github.com/makeplane/plane/issues/new?assignees=srinivaspendem%2Cpushya22&labels=%E2%9C%A8feature&projects=&template=--feature-request.yaml&title=%5Bfeature%5D%3A+).
- Review the [documentation](https://docs.plane.so/) and submit [pull requests](https://github.com/makeplane/docs) to improve it—whether it's fixing typos or adding new content.
- Talk or write about Plane or any other ecosystem integration and [let us know](https://forum.plane.so)!
- Show your support by upvoting [popular feature requests](https://github.com/makeplane/plane/issues).

Please read [CONTRIBUTING.md](https://github.com/makeplane/plane/blob/master/CONTRIBUTING.md) for details on the process for submitting pull requests to us.

### Repo activity

![Plane Repo Activity](https://repobeats.axiom.co/api/embed/2523c6ed2f77c082b7908c33e2ab208981d76c39.svg "Repobeats analytics image")

### We couldn't have done this without you.

<a href="https://github.com/makeplane/plane/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=makeplane/plane" />
</a>

## License

This project is licensed under the [GNU Affero General Public License v3.0](https://github.com/makeplane/plane/blob/master/LICENSE.txt).
