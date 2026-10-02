import { observer } from "mobx-react";
import { useParams } from "react-router";
import { EUserPermissions, EUserPermissionsLevel } from "@plane/constants";
import { Breadcrumbs } from "@plane/ui";
import { NotAuthorizedView } from "@/components/auth-screens/not-authorized-view";
import { BreadcrumbLink } from "@/components/common/breadcrumb-link";
import { PageHead } from "@/components/core/page-title";
import { JiraAPIImport } from "@/components/imports/jira-import";
import { SettingsContentWrapper } from "@/components/settings/content-wrapper";
import { SettingsPageHeader } from "@/components/settings/page-header";
import { useUserPermissions } from "@/hooks/store/user";

async function imported() {}

export default observer(function JiraImportPage() {
  const { workspaceSlug, projectId } = useParams();
  const { workspaceUserInfo, allowPermissions } = useUserPermissions();
  const allowed = allowPermissions([EUserPermissions.ADMIN, EUserPermissions.MEMBER], EUserPermissionsLevel.PROJECT);
  if (workspaceUserInfo && !allowed) return <NotAuthorizedView section="settings" isProjectView className="h-auto" />;
  return (
    <SettingsContentWrapper
      header={
        <SettingsPageHeader
          leftItem={
            <Breadcrumbs>
              <Breadcrumbs.Item component={<BreadcrumbLink label="Jira Import" />} />
            </Breadcrumbs>
          }
        />
      }
    >
      <PageHead title="Jira Import" />
      <div className="space-y-4 p-4">
        <h1 className="text-18 font-medium">Import from Jira Cloud</h1>
        <p className="text-13 text-tertiary">
          Import work items, statuses, labels, parent/subtasks, issue links, comments and attachments. Jira sprints
          become Plane cycles.
        </p>
        {workspaceSlug && projectId && (
          <JiraAPIImport
            key={`${workspaceSlug}:${projectId}`}
            workspaceSlug={workspaceSlug}
            projectId={projectId}
            onImported={imported}
          />
        )}
      </div>
    </SettingsContentWrapper>
  );
});
