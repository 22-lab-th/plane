/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file in the project root for license information.
 */

import { observer } from "mobx-react";
import useSWR from "swr";
// components
import { PageWrapper } from "@/components/common/page-wrapper";
import { Skeleton } from "@/components/common/skeleton";
// hooks
import { useInstance } from "@/hooks/store";
// types
import type { Route } from "./+types/page";
// local
import { InstanceStorageConfigForm } from "./form";

const InstanceStoragePage = observer(function InstanceStoragePage(_props: Route.ComponentProps) {
  const { formattedConfig, fetchInstanceConfigurations } = useInstance();

  useSWR("INSTANCE_CONFIGURATIONS", () => fetchInstanceConfigurations());

  return (
    <PageWrapper
      header={{
        title: "Object storage",
        description: "Configure Amazon S3, Cloudflare R2, or another S3-compatible object storage provider.",
      }}
    >
      {formattedConfig ? (
        <InstanceStorageConfigForm config={formattedConfig} />
      ) : (
        <Skeleton className="space-y-8">
          <Skeleton.Item height="50px" width="50%" />
          <Skeleton.Item height="50px" width="50%" />
          <Skeleton.Item height="50px" width="20%" />
        </Skeleton>
      )}
    </PageWrapper>
  );
});

export const meta: Route.MetaFunction = () => [{ title: "Object Storage - God Mode" }];

export default InstanceStoragePage;
