/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect } from "react";
import { Controller, useForm } from "react-hook-form";
import { Button } from "@plane/propel/button";
import type { TWorkspaceBookmarkGroup } from "@plane/types";
import { Input, ModalCore } from "@plane/ui";

type Props = {
  isOpen: boolean;
  group: TWorkspaceBookmarkGroup | null;
  onClose: () => void;
  onSubmit: (name: string) => Promise<void>;
};

export function WorkspaceBookmarkGroupModal({ isOpen, group, onClose, onSubmit }: Props) {
  const {
    control,
    formState: { errors, isSubmitting },
    handleSubmit,
    reset,
  } = useForm<{ name: string }>({ defaultValues: { name: "" } });

  useEffect(() => {
    if (isOpen) reset({ name: group?.name ?? "" });
  }, [group, isOpen, reset]);

  return (
    <ModalCore isOpen={isOpen} handleClose={onClose}>
      <form onSubmit={handleSubmit((data) => onSubmit(data.name))}>
        <div className="space-y-5 p-5">
          <div>
            <h3 className="text-18 font-medium text-secondary">{group ? "Edit group" : "Add group"}</h3>
            <p className="mt-1 text-12 text-placeholder">Use groups to keep workspace bookmarks easy to browse.</p>
          </div>
          <div>
            <label htmlFor="bookmark-group-name" className="mb-1 block text-13 font-medium text-secondary">
              Group name
            </label>
            <Controller
              control={control}
              name="name"
              rules={{ required: "Group name is required." }}
              render={({ field }) => (
                <Input
                  {...field}
                  id="bookmark-group-name"
                  placeholder="e.g. Engineering"
                  className="w-full"
                  hasError={Boolean(errors.name)}
                />
              )}
            />
            {errors.name && <p className="mt-1 text-11 text-danger-primary">{errors.name.message}</p>}
          </div>
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-subtle px-5 py-4">
          <Button type="button" variant="secondary" size="lg" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" size="lg" loading={isSubmitting}>
            {group ? "Save changes" : "Add group"}
          </Button>
        </div>
      </form>
    </ModalCore>
  );
}
