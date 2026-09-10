# 22lab Project Page Folders

## Goal

Add safe, project-scoped document grouping to Plane v1.4.1 without changing existing page content, assets, Yjs documents, project membership, or public/private ownership semantics.

## Data model

- Reuse `Page.parent` and `Page.sort_order`.
- Add `Page.node_type` with `page` as the migration-safe default and `folder` as the only other value.
- Existing rows remain root pages after migration.
- A folder has no editable document body.

## Required behavior

1. Members with page-create permission can create a folder at root or in another folder.
2. Pages and folders can be moved to root or a folder in the same project.
3. A parent must be an active folder in the same workspace and project.
4. Self-parenting, descendant cycles, cross-project moves, and moves into archived folders are rejected.
5. Nested nodes must use the same access bucket as their parent in this release.
6. Private folders cannot be used or viewed by members other than their owner or project admins.
7. Folder names are non-empty and unique, case-insensitively, among sibling folders in the same access bucket.
8. Root and child lists order folders before pages, then use `sort_order`, name, and a stable identifier.
9. A child page can be retrieved directly.
10. Archiving a folder archives all descendants. Restore preserves a valid hierarchy.
11. A non-empty folder cannot be permanently deleted. Deleting an empty folder is allowed.
12. Project page counts exclude folder nodes.
13. Existing pages, descriptions, images, labels, favorites, archives, and work-item links remain intact.

## User interface

- Create and rename folder dialogs.
- Folder rows in Project Pages.
- Breadcrumbs on folder lists and page detail.
- Move-to-folder/root dialog.
- Expand or navigate into nested folders.
- Drag-and-drop is optional and must have an accessible non-drag alternative.
- Search results must remain usable and retain folder context.

## API and MCP

- Page create/update responses expose `node_type`, `parent`, and `sort_order`.
- Page list accepts a root or parent scope and can list folders.
- Page update supports safe reparenting and ordering.
- The 22lab public Page API exposes the same fields and validations.
- The 22lab Plane MCP supports listing a tree, creating folders, moving/reparenting, and reordering.

## Production migration and rollback

- Take a PostgreSQL backup and record current image tags before deployment.
- Deploy backend migration before enabling the new frontend.
- Backfill is metadata-only: every existing row is `node_type=page`, with existing `parent` and `sort_order` preserved.
- Existing 22lab Pages may be grouped only through an explicit, recorded mapping; content is never rewritten for grouping.
- Rollback restores the previous compose/image tags. Database rollback removes `node_type` only after all folder nodes have been exported or converted to pages.

## Verification

- Backend contract tests cover creation, listing, retrieval, validation, archive/restore, deletion, permissions, and legacy rows.
- Frontend type checks, lint, and build pass.
- MCP unit/dispatch tests pass.
- Production smoke tests verify existing page count/content metadata, folder CRUD, page move/restore, image rendering, and rollback readiness.
