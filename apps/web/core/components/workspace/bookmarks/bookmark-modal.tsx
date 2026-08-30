/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { Button } from "@plane/propel/button";
import type { TWorkspaceBookmark, TWorkspaceBookmarkGroup } from "@plane/types";
import { CustomSelect, Input, ModalCore, TextArea } from "@plane/ui";

type TBookmarkForm = {
  title: string;
  url: string;
  remark: string;
  group: string;
};

type Props = {
  isOpen: boolean;
  bookmark: TWorkspaceBookmark | null;
  groups: TWorkspaceBookmarkGroup[];
  onClose: () => void;
  onSubmit: (data: Partial<TWorkspaceBookmark>) => Promise<void>;
};

const defaultValues: TBookmarkForm = {
  title: "",
  url: "",
  remark: "",
  group: "ungrouped",
};

export function WorkspaceBookmarkModal({ isOpen, bookmark, groups, onClose, onSubmit }: Props) {
  const {
    control,
    formState: { errors, isSubmitting },
    handleSubmit,
    reset,
  } = useForm<TBookmarkForm>({ defaultValues });
  const selectedGroupId = useWatch({ control, name: "group" });

  useEffect(() => {
    if (isOpen) {
      reset({
        title: bookmark?.title ?? "",
        url: bookmark?.url ?? "",
        remark: bookmark?.remark ?? "",
        group: bookmark?.group ?? "ungrouped",
      });
    }
  }, [bookmark, isOpen, reset]);

  const submitForm = async (data: TBookmarkForm) => {
    await onSubmit({
      title: data.title,
      url: data.url,
      remark: data.remark,
      group: data.group === "ungrouped" ? null : data.group,
    });
  };

  const selectedGroupName = groups.find((group) => group.id === selectedGroupId)?.name ?? "Ungrouped";

  return (
    <ModalCore isOpen={isOpen} handleClose={onClose}>
      <form onSubmit={handleSubmit(submitForm)}>
        <div className="space-y-5 p-5">
          <div>
            <h3 className="text-18 font-medium text-secondary">{bookmark ? "Edit bookmark" : "Add bookmark"}</h3>
            <p className="mt-1 text-12 text-placeholder">Share a useful link with everyone in this workspace.</p>
          </div>
          <div className="space-y-4">
            <div>
              <label htmlFor="bookmark-title" className="mb-1 block text-13 font-medium text-secondary">
                Title
              </label>
              <Controller
                control={control}
                name="title"
                rules={{ required: "Title is required." }}
                render={({ field }) => (
                  <Input
                    {...field}
                    id="bookmark-title"
                    placeholder="e.g. Production runbook"
                    className="w-full"
                    hasError={Boolean(errors.title)}
                  />
                )}
              />
              {errors.title && <p className="mt-1 text-11 text-danger-primary">{errors.title.message}</p>}
            </div>
            <div>
              <label htmlFor="bookmark-url" className="mb-1 block text-13 font-medium text-secondary">
                URL
              </label>
              <Controller
                control={control}
                name="url"
                rules={{ required: "URL is required." }}
                render={({ field }) => (
                  <Input
                    {...field}
                    id="bookmark-url"
                    placeholder="https://example.com"
                    className="w-full"
                    hasError={Boolean(errors.url)}
                  />
                )}
              />
              {errors.url && <p className="mt-1 text-11 text-danger-primary">{errors.url.message}</p>}
            </div>
            <div>
              <p className="mb-1 block text-13 font-medium text-secondary">Group</p>
              <Controller
                control={control}
                name="group"
                render={({ field: { value, onChange } }) => (
                  <CustomSelect
                    value={value}
                    onChange={onChange}
                    label={selectedGroupName}
                    buttonClassName="w-full !border-subtle !shadow-none rounded-md"
                    input
                  >
                    <CustomSelect.Option value="ungrouped">Ungrouped</CustomSelect.Option>
                    {groups.map((group) => (
                      <CustomSelect.Option key={group.id} value={group.id}>
                        {group.name}
                      </CustomSelect.Option>
                    ))}
                  </CustomSelect>
                )}
              />
            </div>
            <div>
              <label htmlFor="bookmark-remark" className="mb-1 block text-13 font-medium text-secondary">
                Remark <span className="font-normal text-placeholder">(optional)</span>
              </label>
              <Controller
                control={control}
                name="remark"
                render={({ field }) => (
                  <TextArea
                    {...field}
                    id="bookmark-remark"
                    name="remark"
                    placeholder="Add context, an owner, or usage notes..."
                    className="min-h-24 w-full"
                  />
                )}
              />
            </div>
          </div>
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-subtle px-5 py-4">
          <Button type="button" variant="secondary" size="lg" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" size="lg" loading={isSubmitting}>
            {bookmark ? "Save changes" : "Add bookmark"}
          </Button>
        </div>
      </form>
    </ModalCore>
  );
}
