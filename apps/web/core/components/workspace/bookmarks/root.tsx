/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useMemo, useState } from "react";
import { Bookmark, ExternalLink, Folder, Link2, MoreHorizontal, Pencil, Plus, Search, Trash2, X } from "lucide-react";
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
const ALL_BOOKMARKS = "all";
const UNGROUPED_BOOKMARKS = "ungrouped";

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
  const [activeGroupId, setActiveGroupId] = useState(ALL_BOOKMARKS);
  const [activeBookmark, setActiveBookmark] = useState<TWorkspaceBookmark | null>(null);
  const [bookmarkModalOpen, setBookmarkModalOpen] = useState(false);
  const [groupModalOpen, setGroupModalOpen] = useState(false);
  const [editingBookmark, setEditingBookmark] = useState<TWorkspaceBookmark | null>(null);
  const [editingGroup, setEditingGroup] = useState<TWorkspaceBookmarkGroup | null>(null);
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
    const searchedBookmarks = query
      ? (bookmarks ?? []).filter((bookmark) =>
          [bookmark.title, bookmark.url, bookmark.remark].some((value) => value.toLocaleLowerCase().includes(query))
        )
      : (bookmarks ?? []);
    if (activeGroupId === ALL_BOOKMARKS) return searchedBookmarks;
    if (activeGroupId === UNGROUPED_BOOKMARKS) return searchedBookmarks.filter((bookmark) => bookmark.group === null);
    return searchedBookmarks.filter((bookmark) => bookmark.group === activeGroupId);
  }, [activeGroupId, bookmarks, searchQuery]);

  const activeGroupName = useMemo(() => {
    if (activeGroupId === ALL_BOOKMARKS) return "All bookmarks";
    if (activeGroupId === UNGROUPED_BOOKMARKS) return "Ungrouped";
    return groups?.find((group) => group.id === activeGroupId)?.name ?? "Bookmarks";
  }, [activeGroupId, groups]);

  const activeBookmarkGroupName = activeBookmark?.group
    ? (groups?.find((group) => group.id === activeBookmark.group)?.name ?? "Unknown group")
    : "Ungrouped";

  useEffect(() => {
    if (activeBookmark && !visibleBookmarks.some((bookmark) => bookmark.id === activeBookmark.id))
      setActiveBookmark(null);
  }, [activeBookmark, visibleBookmarks]);

  const showError = (message: string) => setToast({ type: TOAST_TYPE.ERROR, title: "Something went wrong", message });

  const handleBookmarkSubmit = async (data: Partial<TWorkspaceBookmark>) => {
    try {
      if (editingBookmark) await workspaceService.updateWorkspaceBookmark(slug, editingBookmark.id, data);
      else await workspaceService.createWorkspaceBookmark(slug, data);
      await mutateBookmarks();
      if (activeBookmark && editingBookmark?.id === activeBookmark.id)
        setActiveBookmark({ ...activeBookmark, ...data });
      setBookmarkModalOpen(false);
      setEditingBookmark(null);
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: editingBookmark ? "Bookmark updated" : "Bookmark added",
        message: "The shared workspace bookmark is ready for everyone.",
      });
    } catch {
      showError("The bookmark could not be saved. Check the URL and try again.");
      throw new Error("Bookmark save failed");
    }
  };

  const handleGroupSubmit = async (name: string) => {
    try {
      if (editingGroup) await workspaceService.updateWorkspaceBookmarkGroup(slug, editingGroup.id, { name });
      else await workspaceService.createWorkspaceBookmarkGroup(slug, { name });
      await mutateGroups();
      setGroupModalOpen(false);
      setEditingGroup(null);
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: editingGroup ? "Group updated" : "Group added",
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
        if (activeBookmark?.id === deleteTarget.value.id) setActiveBookmark(null);
        await mutateBookmarks();
      } else {
        await workspaceService.deleteWorkspaceBookmarkGroup(slug, deleteTarget.value.id);
        if (activeGroupId === deleteTarget.value.id) setActiveGroupId(ALL_BOOKMARKS);
        setActiveBookmark(null);
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
    setEditingBookmark(bookmark);
    setBookmarkModalOpen(true);
  };
  const openGroupModal = (group: TWorkspaceBookmarkGroup | null = null) => {
    setEditingGroup(group);
    setGroupModalOpen(true);
  };
  const selectGroup = (groupId: string) => {
    setActiveGroupId(groupId);
    setActiveBookmark(null);
  };

  if (groupsLoading || bookmarksLoading)
    return (
      <div className="grid h-full place-items-center">
        <Spinner />
      </div>
    );

  const groupItems = [
    { id: ALL_BOOKMARKS, name: "All bookmarks", count: bookmarks?.length ?? 0 },
    ...(groups ?? []).map((group) => ({
      id: group.id,
      name: group.name,
      count: bookmarks?.filter((bookmark) => bookmark.group === group.id).length ?? 0,
      group,
    })),
    {
      id: UNGROUPED_BOOKMARKS,
      name: "Ungrouped",
      count: bookmarks?.filter((bookmark) => bookmark.group === null).length ?? 0,
    },
  ];

  return (
    <div className="relative flex h-full min-h-0 overflow-hidden bg-surface-1">
      <aside className="hidden w-52 flex-shrink-0 flex-col border-r border-subtle bg-surface-1 md:flex xl:w-56">
        <div className="flex items-center justify-between border-b border-subtle px-3 py-3">
          <span className="text-12 font-medium text-placeholder">Groups</span>
          {canManage && (
            <button
              type="button"
              className="grid size-7 place-items-center rounded text-placeholder hover:bg-layer-1 hover:text-secondary"
              onClick={() => openGroupModal()}
              aria-label="Add group"
            >
              <Plus className="size-4" />
            </button>
          )}
        </div>
        <nav className="min-h-0 flex-1 space-y-0.5 overflow-y-auto p-2" aria-label="Bookmark groups">
          {groupItems.map((item) => {
            const isActive = activeGroupId === item.id;
            const isEditableGroup = "group" in item && item.group;
            return (
              <div
                key={item.id}
                className={`group flex min-h-9 items-center rounded-md ${isActive ? "bg-accent-subtle" : "hover:bg-layer-1"}`}
              >
                <button
                  type="button"
                  className={`flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left text-13 ${isActive ? "font-medium text-accent-primary" : "text-secondary"}`}
                  onClick={() => selectGroup(item.id)}
                >
                  {item.id === ALL_BOOKMARKS ? (
                    <Bookmark className="size-4 flex-shrink-0" />
                  ) : (
                    <Folder className="size-4 flex-shrink-0" />
                  )}
                  <span className="min-w-0 flex-1 truncate">{item.name}</span>
                  <span className="text-11 text-placeholder">{item.count}</span>
                </button>
                {canManage && isEditableGroup && (
                  <div className="pr-1 opacity-0 group-hover:opacity-100">
                    <CustomMenu
                      customButton={
                        <span className="grid size-6 place-items-center rounded text-placeholder hover:bg-surface-1">
                          <MoreHorizontal className="size-3.5" />
                        </span>
                      }
                      placement="bottom-start"
                      closeOnSelect
                    >
                      <CustomMenu.MenuItem onClick={() => openGroupModal(item.group)}>
                        <span className="flex items-center gap-2">
                          <Pencil className="size-3.5" />
                          Edit group
                        </span>
                      </CustomMenu.MenuItem>
                      <CustomMenu.MenuItem onClick={() => setDeleteTarget({ type: "group", value: item.group })}>
                        <span className="flex items-center gap-2 text-danger-primary">
                          <Trash2 className="size-3.5" />
                          Delete group
                        </span>
                      </CustomMenu.MenuItem>
                    </CustomMenu>
                  </div>
                )}
              </div>
            );
          })}
        </nav>
        {canManage && (
          <div className="border-t border-subtle p-3">
            <Button
              variant="secondary"
              size="base"
              prependIcon={<Plus className="size-4" />}
              className="w-full justify-center"
              onClick={() => openGroupModal()}
            >
              Add group
            </Button>
          </div>
        )}
      </aside>

      <main className="flex min-w-0 flex-1 flex-col bg-surface-1">
        <div className="flex flex-col gap-3 border-b border-subtle px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="relative w-full lg:max-w-sm">
            <Search className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-placeholder" />
            <Input
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Search bookmarks, URLs, or remarks..."
              className="w-full pl-9"
            />
          </div>
          {canManage && (
            <Button
              variant="primary"
              size="lg"
              prependIcon={<Plus className="size-4" />}
              onClick={() => openBookmarkModal()}
            >
              Add bookmark
            </Button>
          )}
        </div>

        <div className="flex gap-1 overflow-x-auto border-b border-subtle px-3 py-2 md:hidden">
          {groupItems.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`flex flex-shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1.5 text-12 ${activeGroupId === item.id ? "bg-accent-subtle font-medium text-accent-primary" : "text-secondary"}`}
              onClick={() => selectGroup(item.id)}
            >
              {item.name}
              <span className="text-11 text-placeholder">{item.count}</span>
            </button>
          ))}
        </div>

        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex items-center justify-between px-4 py-4">
            <div>
              <h1 className="text-15 font-semibold text-secondary">{activeGroupName}</h1>
              <p className="mt-0.5 text-11 text-placeholder">
                {visibleBookmarks.length} {visibleBookmarks.length === 1 ? "bookmark" : "bookmarks"}
              </p>
            </div>
          </div>
          <div className="hidden grid-cols-[minmax(0,1.1fr)_minmax(120px,.55fr)_minmax(0,.9fr)_2rem] gap-3 border-y border-subtle bg-layer-1 px-4 py-2 text-11 font-medium tracking-wide text-placeholder uppercase xl:grid">
            <span>Title</span>
            <span>Hostname</span>
            <span>Remark</span>
            <span className="sr-only">Actions</span>
          </div>

          {visibleBookmarks.length === 0 ? (
            <div className="grid min-h-0 flex-1 place-items-center px-6 py-16 text-center">
              <div>
                <Bookmark className="mx-auto size-7 text-placeholder" />
                <p className="mt-3 text-14 font-medium text-secondary">
                  {searchQuery ? "No bookmarks match your search" : "No bookmarks in this group"}
                </p>
                <p className="mt-1 text-12 text-placeholder">
                  {searchQuery ? "Try another title, URL, or remark." : "Choose another group or add a bookmark."}
                </p>
              </div>
            </div>
          ) : (
            <div className="min-h-0 flex-1 overflow-y-auto">
              {visibleBookmarks.map((bookmark) => {
                const isActive = activeBookmark?.id === bookmark.id;
                return (
                  <div
                    key={bookmark.id}
                    className={`group relative grid grid-cols-[minmax(0,1fr)_2rem] gap-3 border-b border-subtle px-4 py-3.5 transition-colors xl:grid-cols-[minmax(0,1.1fr)_minmax(120px,.55fr)_minmax(0,.9fr)_2rem] ${isActive ? "bg-accent-subtle" : "hover:bg-layer-1"}`}
                  >
                    <button
                      type="button"
                      className="focus-visible:ring-accent-primary absolute inset-0 z-0 cursor-pointer outline-none focus-visible:ring-1 focus-visible:ring-inset"
                      onClick={() => setActiveBookmark(bookmark)}
                      aria-label={`Show details for ${bookmark.title}`}
                    />
                    <div className="pointer-events-none relative z-[1] flex min-w-0 items-start gap-2.5">
                      <Bookmark
                        className={`mt-0.5 size-4 flex-shrink-0 ${isActive ? "text-accent-primary" : "text-placeholder"}`}
                      />
                      <div className="min-w-0">
                        <h2
                          className={`truncate text-13 font-medium ${isActive ? "text-accent-primary" : "text-secondary"}`}
                        >
                          {bookmark.title}
                        </h2>
                        <p className="mt-0.5 truncate text-11 text-placeholder xl:hidden">
                          {getHostname(bookmark.url)}
                        </p>
                        <p className="mt-1 line-clamp-1 text-12 text-tertiary xl:hidden">
                          {bookmark.remark || "No remark"}
                        </p>
                      </div>
                    </div>
                    <p className="pointer-events-none relative z-[1] hidden self-center truncate text-12 text-placeholder xl:block">
                      {getHostname(bookmark.url)}
                    </p>
                    <p
                      className={`pointer-events-none relative z-[1] hidden self-center truncate text-12 xl:block ${bookmark.remark ? "text-tertiary" : "text-placeholder"}`}
                    >
                      {bookmark.remark || "No remark"}
                    </p>
                    {canManage ? (
                      <div className="relative z-[2] self-center">
                        <CustomMenu
                          customButton={
                            <span className="grid size-7 place-items-center rounded text-placeholder opacity-60 group-hover:opacity-100 hover:bg-surface-1 hover:text-secondary">
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
                          <CustomMenu.MenuItem onClick={() => setDeleteTarget({ type: "bookmark", value: bookmark })}>
                            <span className="flex items-center gap-2 text-danger-primary">
                              <Trash2 className="size-3.5" />
                              Delete
                            </span>
                          </CustomMenu.MenuItem>
                        </CustomMenu>
                      </div>
                    ) : (
                      <span />
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </main>

      {activeBookmark && (
        <aside className="absolute inset-0 z-20 flex min-w-0 flex-col border-l border-subtle bg-surface-1 md:static md:w-80 md:flex-shrink-0 2xl:w-96">
          <div className="flex h-14 flex-shrink-0 items-center justify-between border-b border-subtle px-4">
            <div className="flex items-center gap-2 text-13 font-medium text-secondary">
              <Bookmark className="size-4 text-placeholder" />
              Bookmark details
            </div>
            <button
              type="button"
              className="grid size-8 place-items-center rounded text-placeholder hover:bg-layer-1 hover:text-secondary"
              onClick={() => setActiveBookmark(null)}
              aria-label="Close bookmark details"
            >
              <X className="size-4" />
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
            <h2 className="text-18 leading-6 font-semibold text-secondary">{activeBookmark.title}</h2>
            <a
              href={activeBookmark.url}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-3 flex items-center gap-2 text-12 font-medium text-accent-primary hover:underline"
            >
              <Link2 className="size-4" />
              <span className="min-w-0 truncate">{getHostname(activeBookmark.url)}</span>
              <ExternalLink className="size-3.5 flex-shrink-0" />
            </a>
            <div className="mt-7 border-b border-subtle pb-6">
              <p className="text-11 font-medium tracking-wide text-placeholder uppercase">Remark</p>
              <p
                className={`mt-2 text-13 leading-5 whitespace-pre-wrap ${activeBookmark.remark ? "text-secondary" : "text-placeholder"}`}
              >
                {activeBookmark.remark || "No remark"}
              </p>
            </div>
            <div className="border-b border-subtle py-6">
              <p className="text-11 font-medium tracking-wide text-placeholder uppercase">Group</p>
              <div className="mt-2 flex items-center gap-2 text-13 text-secondary">
                <Folder className="size-4 text-placeholder" />
                {activeBookmarkGroupName}
              </div>
            </div>
            <div className="pt-6">
              <p className="text-11 font-medium tracking-wide text-placeholder uppercase">Actions</p>
              <div className="mt-3 space-y-2">
                <Button
                  variant="primary"
                  size="lg"
                  className="w-full justify-center"
                  appendIcon={<ExternalLink className="size-4" />}
                  onClick={() => window.open(activeBookmark.url, "_blank", "noopener,noreferrer")}
                >
                  Open bookmark
                </Button>
                {canManage && (
                  <>
                    <Button
                      variant="secondary"
                      size="lg"
                      className="w-full justify-center"
                      prependIcon={<Pencil className="size-4" />}
                      onClick={() => openBookmarkModal(activeBookmark)}
                    >
                      Edit bookmark
                    </Button>
                    <button
                      type="button"
                      className="flex w-full items-center justify-center gap-2 rounded-md px-3 py-2 text-12 font-medium text-danger-primary hover:bg-danger-subtle"
                      onClick={() => setDeleteTarget({ type: "bookmark", value: activeBookmark })}
                    >
                      <Trash2 className="size-4" />
                      Delete bookmark
                    </button>
                  </>
                )}
              </div>
            </div>
          </div>
        </aside>
      )}

      <WorkspaceBookmarkModal
        isOpen={bookmarkModalOpen}
        bookmark={editingBookmark}
        groups={groups ?? []}
        onClose={() => {
          setBookmarkModalOpen(false);
          setEditingBookmark(null);
        }}
        onSubmit={handleBookmarkSubmit}
        onFetchMetadata={(url) => workspaceService.fetchWorkspaceBookmarkMetadata(slug, url)}
      />
      <WorkspaceBookmarkGroupModal
        isOpen={groupModalOpen}
        group={editingGroup}
        onClose={() => {
          setGroupModalOpen(false);
          setEditingGroup(null);
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
