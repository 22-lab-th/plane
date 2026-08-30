/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useMemo, useState } from "react";
import { MoreHorizontal, Pencil, Plus, Search, Trash2 } from "lucide-react";
import { useParams } from "next/navigation";
import useSWR from "swr";
import { EUserPermissions, EUserPermissionsLevel } from "@plane/constants";
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import type { TWorkspaceBookmark, TWorkspaceBookmarkGroup } from "@plane/types";
import { AlertModalCore, CustomMenu, Input, Spinner } from "@plane/ui";
import { useUserPermissions } from "@/hooks/store/user";
import { WorkspaceService } from "@/services/workspace.service";
import { WorkspaceBookmarkModal } from "./bookmark-modal";
import { WorkspaceBookmarkGroupModal } from "./group-modal";

const workspaceService = new WorkspaceService();

type TDeleteTarget =
  | { type: "bookmark"; value: TWorkspaceBookmark }
  | { type: "group"; value: TWorkspaceBookmarkGroup };

const getHostname = (url: string) => {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
};

export function WorkspaceBookmarksRoot() {
  const { workspaceSlug } = useParams();
  const slug = workspaceSlug?.toString() ?? "";
  const { allowPermissions } = useUserPermissions();
  const canManage = allowPermissions(
    [EUserPermissions.ADMIN, EUserPermissions.MEMBER],
    EUserPermissionsLevel.WORKSPACE,
    slug
  );
  const [searchQuery, setSearchQuery] = useState("");
  const [bookmarkModalOpen, setBookmarkModalOpen] = useState(false);
  const [groupModalOpen, setGroupModalOpen] = useState(false);
  const [selectedBookmark, setSelectedBookmark] = useState<TWorkspaceBookmark | null>(null);
  const [selectedGroup, setSelectedGroup] = useState<TWorkspaceBookmarkGroup | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<TDeleteTarget | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  const {
    data: groups,
    isLoading: groupsLoading,
    mutate: mutateGroups,
  } = useSWR(slug ? `WORKSPACE_BOOKMARK_GROUPS_${slug}` : null, () =>
    workspaceService.fetchWorkspaceBookmarkGroups(slug)
  );
  const {
    data: bookmarks,
    isLoading: bookmarksLoading,
    mutate: mutateBookmarks,
  } = useSWR(slug ? `WORKSPACE_BOOKMARKS_${slug}` : null, () => workspaceService.fetchWorkspaceBookmarks(slug));

  const visibleBookmarks = useMemo(() => {
    const query = searchQuery.trim().toLocaleLowerCase();
    if (!query) return bookmarks ?? [];
    return (bookmarks ?? []).filter((bookmark) =>
      [bookmark.title, bookmark.url, bookmark.remark].some((value) => value.toLocaleLowerCase().includes(query))
    );
  }, [bookmarks, searchQuery]);

  const groupedBookmarks = useMemo(() => {
    const sections = (groups ?? []).map((group) => ({
      group,
      bookmarks: visibleBookmarks.filter((bookmark) => bookmark.group === group.id),
    }));
    const ungrouped = visibleBookmarks.filter((bookmark) => !bookmark.group);
    return [...sections, { group: null, bookmarks: ungrouped }].filter(
      (section) => section.bookmarks.length > 0 || (!searchQuery && section.group)
    );
  }, [groups, searchQuery, visibleBookmarks]);

  const showError = (message: string) => setToast({ type: TOAST_TYPE.ERROR, title: "Something went wrong", message });

  const handleBookmarkSubmit = async (data: Partial<TWorkspaceBookmark>) => {
    try {
      if (selectedBookmark) await workspaceService.updateWorkspaceBookmark(slug, selectedBookmark.id, data);
      else await workspaceService.createWorkspaceBookmark(slug, data);
      await mutateBookmarks();
      setBookmarkModalOpen(false);
      setSelectedBookmark(null);
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: selectedBookmark ? "Bookmark updated" : "Bookmark added",
        message: "The shared workspace bookmark is ready for everyone.",
      });
    } catch {
      showError("The bookmark could not be saved. Check the URL and try again.");
      throw new Error("Bookmark save failed");
    }
  };

  const handleGroupSubmit = async (name: string) => {
    try {
      if (selectedGroup) await workspaceService.updateWorkspaceBookmarkGroup(slug, selectedGroup.id, { name });
      else await workspaceService.createWorkspaceBookmarkGroup(slug, { name });
      await mutateGroups();
      setGroupModalOpen(false);
      setSelectedGroup(null);
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: selectedGroup ? "Group updated" : "Group added",
        message: "Bookmark groups are shared across this workspace.",
      });
    } catch {
      showError("The group could not be saved. Its name may already be in use.");
      throw new Error("Bookmark group save failed");
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setIsDeleting(true);
    try {
      if (deleteTarget.type === "bookmark") {
        await workspaceService.deleteWorkspaceBookmark(slug, deleteTarget.value.id);
        await mutateBookmarks();
      } else {
        await workspaceService.deleteWorkspaceBookmarkGroup(slug, deleteTarget.value.id);
        await Promise.all([mutateGroups(), mutateBookmarks()]);
      }
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: deleteTarget.type === "bookmark" ? "Bookmark deleted" : "Group deleted",
        message:
          deleteTarget.type === "bookmark"
            ? "The bookmark was removed from this workspace."
            : "Bookmarks from this group were moved to Ungrouped.",
      });
      setDeleteTarget(null);
    } catch {
      showError("The item could not be deleted. Please try again.");
    } finally {
      setIsDeleting(false);
    }
  };

  const openBookmarkModal = (bookmark: TWorkspaceBookmark | null = null) => {
    setSelectedBookmark(bookmark);
    setBookmarkModalOpen(true);
  };

  const openGroupModal = (group: TWorkspaceBookmarkGroup | null = null) => {
    setSelectedGroup(group);
    setGroupModalOpen(true);
  };

  if (groupsLoading || bookmarksLoading)
    return (
      <div className="grid h-full place-items-center">
        <Spinner />
      </div>
    );

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-6">
        <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
          <div className="relative w-full sm:max-w-sm">
            <Search className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-placeholder" />
            <Input
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Search bookmarks, URLs, or remarks..."
              className="w-full pl-9"
            />
          </div>
          {canManage && (
            <div className="flex items-center gap-2">
              <Button
                variant="secondary"
                size="lg"
                prependIcon={<Plus className="size-4" />}
                onClick={() => openGroupModal()}
              >
                Add group
              </Button>
              <Button
                variant="primary"
                size="lg"
                prependIcon={<Plus className="size-4" />}
                onClick={() => openBookmarkModal()}
              >
                Add bookmark
              </Button>
            </div>
          )}
        </div>

        {groupedBookmarks.length === 0 ? (
          <div className="rounded-lg border border-dashed border-subtle px-6 py-16 text-center">
            <p className="text-15 font-medium text-secondary">
              {searchQuery ? "No bookmarks match your search" : "No workspace bookmarks yet"}
            </p>
            <p className="mt-1 text-13 text-placeholder">
              {searchQuery
                ? "Try another title, URL, or remark."
                : canManage
                  ? "Add a useful link so everyone can find it here."
                  : "Workspace members have not added any shared links yet."}
            </p>
          </div>
        ) : (
          <div className="space-y-7">
            {groupedBookmarks.map(({ group, bookmarks: sectionBookmarks }) => (
              <section key={group?.id ?? "ungrouped"}>
                <div className="mb-3 flex min-h-7 items-center justify-between">
                  <div className="flex items-baseline gap-2">
                    <h2 className="text-15 font-semibold text-secondary">{group?.name ?? "Ungrouped"}</h2>
                    <span className="text-12 text-placeholder">{sectionBookmarks.length}</span>
                  </div>
                  {canManage && group && (
                    <CustomMenu
                      customButton={
                        <span className="grid size-7 place-items-center rounded-sm text-placeholder hover:bg-layer-1">
                          <MoreHorizontal className="size-4" />
                        </span>
                      }
                      placement="bottom-end"
                      closeOnSelect
                    >
                      <CustomMenu.MenuItem onClick={() => openGroupModal(group)}>
                        <span className="flex items-center gap-2">
                          <Pencil className="size-3.5" />
                          Edit group
                        </span>
                      </CustomMenu.MenuItem>
                      <CustomMenu.MenuItem onClick={() => setDeleteTarget({ type: "group", value: group })}>
                        <span className="flex items-center gap-2 text-danger-primary">
                          <Trash2 className="size-3.5" />
                          Delete group
                        </span>
                      </CustomMenu.MenuItem>
                    </CustomMenu>
                  )}
                </div>

                {sectionBookmarks.length === 0 ? (
                  <div className="rounded-md border border-dashed border-subtle px-4 py-6 text-center text-12 text-placeholder">
                    No bookmarks in this group.
                  </div>
                ) : (
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                    {sectionBookmarks.map((bookmark) => (
                      <article
                        key={bookmark.id}
                        className="group flex min-h-32 flex-col rounded-lg border border-subtle bg-surface-1 p-4 transition-colors hover:border-strong"
                      >
                        <div className="flex items-start justify-between gap-3">
                          <button
                            type="button"
                            className="min-w-0 flex-1 text-left"
                            onClick={() => window.open(bookmark.url, "_blank", "noopener,noreferrer")}
                          >
                            <h3 className="truncate text-14 font-semibold text-secondary group-hover:text-accent-primary">
                              {bookmark.title}
                            </h3>
                            <p className="mt-0.5 truncate text-11 text-placeholder">{getHostname(bookmark.url)}</p>
                          </button>
                          {canManage && (
                            <CustomMenu
                              customButton={
                                <span className="grid size-7 place-items-center rounded-sm text-placeholder hover:bg-layer-1">
                                  <MoreHorizontal className="size-4" />
                                </span>
                              }
                              placement="bottom-end"
                              closeOnSelect
                            >
                              <CustomMenu.MenuItem onClick={() => openBookmarkModal(bookmark)}>
                                <span className="flex items-center gap-2">
                                  <Pencil className="size-3.5" />
                                  Edit
                                </span>
                              </CustomMenu.MenuItem>
                              <CustomMenu.MenuItem
                                onClick={() => setDeleteTarget({ type: "bookmark", value: bookmark })}
                              >
                                <span className="flex items-center gap-2 text-danger-primary">
                                  <Trash2 className="size-3.5" />
                                  Delete
                                </span>
                              </CustomMenu.MenuItem>
                            </CustomMenu>
                          )}
                        </div>
                        {bookmark.remark ? (
                          <p className="mt-3 line-clamp-3 text-12 leading-5 text-tertiary">{bookmark.remark}</p>
                        ) : (
                          <p className="mt-3 text-12 text-placeholder">No remark</p>
                        )}
                        <button
                          type="button"
                          className="mt-auto pt-3 text-left text-12 font-medium text-accent-primary"
                          onClick={() => window.open(bookmark.url, "_blank", "noopener,noreferrer")}
                        >
                          Open bookmark ↗
                        </button>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            ))}
          </div>
        )}
      </div>

      <WorkspaceBookmarkModal
        isOpen={bookmarkModalOpen}
        bookmark={selectedBookmark}
        groups={groups ?? []}
        onClose={() => {
          setBookmarkModalOpen(false);
          setSelectedBookmark(null);
        }}
        onSubmit={handleBookmarkSubmit}
      />
      <WorkspaceBookmarkGroupModal
        isOpen={groupModalOpen}
        group={selectedGroup}
        onClose={() => {
          setGroupModalOpen(false);
          setSelectedGroup(null);
        }}
        onSubmit={handleGroupSubmit}
      />
      <AlertModalCore
        isOpen={Boolean(deleteTarget)}
        isSubmitting={isDeleting}
        handleClose={() => setDeleteTarget(null)}
        handleSubmit={handleDelete}
        title={deleteTarget?.type === "group" ? "Delete bookmark group?" : "Delete bookmark?"}
        content={
          deleteTarget?.type === "group" ? (
            <>The group will be deleted. Its bookmarks will remain available under Ungrouped.</>
          ) : (
            <>This bookmark will no longer be available to workspace members.</>
          )
        }
      />
    </div>
  );
}
