/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { unset, set } from "lodash-es";
import { makeObservable, observable, runInAction, action, reaction, computed } from "mobx";
import { computedFn } from "mobx-utils";
// types
import { EUserPermissions } from "@plane/constants";
import type { TPage, TPageFilters, TPageNavigationTabs } from "@plane/types";
import { EUserProjectRoles } from "@plane/types";
// helpers
import { filterPagesByPageType, getPageName, orderPages, shouldFilterPage } from "@plane/utils";
// plane web store
// services
import { ProjectPageService } from "@/services/page";
// store
import type { CoreRootStore } from "../root.store";
import type { TProjectPage } from "./project-page";
import { ProjectPage } from "./project-page";

type TLoader = "init-loader" | "mutation-loader" | undefined;

type TError = { title: string; description: string };

export const ROLE_PERMISSIONS_TO_CREATE_PAGE = [
  EUserPermissions.ADMIN,
  EUserPermissions.MEMBER,
  EUserProjectRoles.ADMIN,
  EUserProjectRoles.MEMBER,
];

export interface IProjectPageStore {
  // observables
  loader: TLoader;
  data: Record<string, TProjectPage>; // pageId => Page
  error: TError | undefined;
  filters: TPageFilters;
  // computed
  isAnyPageAvailable: boolean;
  canCurrentUserCreatePage: boolean;
  // helper actions
  getCurrentProjectPageIdsByTab: (pageType: TPageNavigationTabs, parentId?: string | null) => string[] | undefined;
  getCurrentProjectPageIds: (projectId: string) => string[];
  getCurrentProjectFilteredPageIdsByTab: (
    pageType: TPageNavigationTabs,
    parentId?: string | null
  ) => string[] | undefined;
  getPageById: (pageId: string) => TProjectPage | undefined;
  getFolderBreadcrumbs: (folderId: string | null | undefined) => TProjectPage[];
  getMoveTargetFolders: (projectId: string, access: number | undefined, excludeId?: string) => TProjectPage[];
  updateFilters: <T extends keyof TPageFilters>(filterKey: T, filterValue: TPageFilters[T]) => void;
  clearAllFilters: () => void;
  // actions
  fetchPagesList: (
    workspaceSlug: string,
    projectId: string,
    pageType?: TPageNavigationTabs,
    parentId?: string | null
  ) => Promise<TPage[] | undefined>;
  fetchPageDetails: (
    workspaceSlug: string,
    projectId: string,
    pageId: string,
    options?: { trackVisit?: boolean }
  ) => Promise<TPage | undefined>;
  createPage: (pageData: Partial<TPage>) => Promise<TPage | undefined>;
  createFolder: (pageData: Partial<TPage>) => Promise<TPage | undefined>;
  moveToFolder: (pageId: string, parentId: string | null) => Promise<void>;
  removePage: (params: { pageId: string; shouldSync?: boolean }) => Promise<void>;
  movePage: (workspaceSlug: string, projectId: string, pageId: string, newProjectId: string) => Promise<void>;
}

export class ProjectPageStore implements IProjectPageStore {
  // observables
  loader: TLoader = "init-loader";
  data: Record<string, TProjectPage> = {}; // pageId => Page
  error: TError | undefined = undefined;
  filters: TPageFilters = {
    searchQuery: "",
    sortKey: "updated_at",
    sortBy: "desc",
  };
  // service
  service: ProjectPageService;
  rootStore: CoreRootStore;

  constructor(private store: CoreRootStore) {
    makeObservable(this, {
      // observables
      loader: observable.ref,
      data: observable,
      error: observable,
      filters: observable,
      // computed
      isAnyPageAvailable: computed,
      canCurrentUserCreatePage: computed,
      // helper actions
      updateFilters: action,
      clearAllFilters: action,
      // actions
      fetchPagesList: action,
      fetchPageDetails: action,
      createPage: action,
      createFolder: action,
      moveToFolder: action,
      removePage: action,
      movePage: action,
    });
    this.rootStore = store;
    // service
    this.service = new ProjectPageService();
    // initialize display filters of the current project
    reaction(
      () => this.store.router.projectId,
      (projectId) => {
        if (!projectId) return;
        this.filters.searchQuery = "";
      }
    );
  }

  /**
   * @description check if any page is available
   */
  get isAnyPageAvailable() {
    if (this.loader) return true;
    return Object.keys(this.data).length > 0;
  }

  /**
   * @description returns true if the current logged in user can create a page
   */
  get canCurrentUserCreatePage() {
    const { workspaceSlug, projectId } = this.store.router;
    const currentUserProjectRole = this.store.user.permission.getProjectRoleByWorkspaceSlugAndProjectId(
      workspaceSlug?.toString() || "",
      projectId?.toString() || ""
    );
    return !!currentUserProjectRole && ROLE_PERMISSIONS_TO_CREATE_PAGE.includes(currentUserProjectRole);
  }

  private matchesParent = (page: TProjectPage, parentId?: string | null) => {
    const expected = parentId ?? null;
    const actual = page.parent ?? null;
    return actual === expected;
  };

  private upsertPages = (pages: TPage[]) => {
    for (const page of pages) {
      if (!page?.id) continue;
      const existingPage = this.getPageById(page.id);
      if (existingPage) {
        const { name: _name, ...otherFields } = page;
        existingPage.mutateProperties(otherFields, false);
      } else {
        set(this.data, [page.id], new ProjectPage(this.store, page));
      }
    }
  };

  /**
   * @description get the current project page ids based on the pageType
   * @param {TPageNavigationTabs} pageType
   */
  getCurrentProjectPageIdsByTab = computedFn((pageType: TPageNavigationTabs, parentId?: string | null) => {
    const { projectId } = this.store.router;
    if (!projectId) return undefined;
    // helps to filter pages based on the pageType
    let pagesByType = filterPagesByPageType(pageType, Object.values(this?.data || {}));
    pagesByType = pagesByType.filter(
      (p) => p.project_ids?.includes(projectId) && this.matchesParent(p as TProjectPage, parentId)
    );

    const pages = (pagesByType.map((page) => page.id) as string[]) || undefined;

    return pages ?? undefined;
  });

  /**
   * @description get the current project page ids
   * @param {string} projectId
   */
  getCurrentProjectPageIds = computedFn((projectId: string) => {
    if (!projectId) return [];
    const pages = Object.values(this?.data || {}).filter((page) => page.project_ids?.includes(projectId));
    return pages.map((page) => page.id) as string[];
  });

  /**
   * @description get the current project filtered page ids based on the pageType
   * @param {TPageNavigationTabs} pageType
   */
  getCurrentProjectFilteredPageIdsByTab = computedFn((pageType: TPageNavigationTabs, parentId?: string | null) => {
    const { projectId } = this.store.router;
    if (!projectId) return undefined;

    // helps to filter pages based on the pageType
    const pagesByType = filterPagesByPageType(pageType, Object.values(this?.data || {}));
    let filteredPages = pagesByType.filter(
      (p) =>
        p.project_ids?.includes(projectId) &&
        this.matchesParent(p as TProjectPage, parentId) &&
        getPageName(p.name).toLowerCase().includes(this.filters.searchQuery.toLowerCase()) &&
        shouldFilterPage(p, this.filters.filters)
    );
    // Folders first, then existing sort.
    filteredPages = [
      ...orderPages(
        filteredPages.filter((p) => p.node_type === "folder"),
        "name",
        "asc"
      ),
      ...orderPages(
        filteredPages.filter((p) => p.node_type !== "folder"),
        this.filters.sortKey,
        this.filters.sortBy
      ),
    ];

    const pages = (filteredPages.map((page) => page.id) as string[]) || undefined;

    return pages ?? undefined;
  });

  /**
   * @description get the page store by id
   * @param {string} pageId
   */
  getPageById = computedFn((pageId: string) => this.data?.[pageId] || undefined);

  getFolderBreadcrumbs = computedFn((folderId: string | null | undefined) => {
    if (!folderId) return [];
    const crumbs: TProjectPage[] = [];
    let current: TProjectPage | undefined = this.getPageById(folderId);
    const seen = new Set<string>();
    while (current?.id && !seen.has(current.id)) {
      seen.add(current.id);
      crumbs.unshift(current);
      current = current.parent ? this.getPageById(current.parent) : undefined;
    }
    return crumbs;
  });

  getMoveTargetFolders = computedFn((projectId: string, access: number | undefined, excludeId?: string) => {
    return Object.values(this.data)
      .filter(
        (page) =>
          page.node_type === "folder" &&
          page.project_ids?.includes(projectId) &&
          !page.archived_at &&
          (access === undefined || page.access === access) &&
          page.id !== excludeId
      )
      .toSorted((a, b) => getPageName(a.name).localeCompare(getPageName(b.name)));
  });

  updateFilters = <T extends keyof TPageFilters>(filterKey: T, filterValue: TPageFilters[T]) => {
    runInAction(() => {
      set(this.filters, [filterKey], filterValue);
    });
  };

  /**
   * @description clear all the filters
   */
  clearAllFilters = () =>
    runInAction(() => {
      set(this.filters, ["filters"], {});
    });

  /**
   * @description fetch pages for a folder level (null/undefined = root)
   */
  fetchPagesList = async (
    workspaceSlug: string,
    projectId: string,
    pageType?: TPageNavigationTabs,
    parentId?: string | null
  ) => {
    try {
      if (!workspaceSlug || !projectId) return undefined;

      const currentPageIds = pageType ? this.getCurrentProjectPageIdsByTab(pageType, parentId) : undefined;
      runInAction(() => {
        this.loader = currentPageIds && currentPageIds.length > 0 ? `mutation-loader` : `init-loader`;
        this.error = undefined;
      });

      const pages = await this.service.fetchAll(workspaceSlug, projectId, parentId ?? null);
      const folders = await this.service.fetchAll(workspaceSlug, projectId, null, { foldersOnly: true });
      runInAction(() => {
        this.upsertPages(pages);
        this.upsertPages(folders);
        this.loader = undefined;
      });

      return pages;
    } catch (error) {
      runInAction(() => {
        this.loader = undefined;
        this.error = {
          title: "Failed",
          description: "Failed to fetch the pages, Please try again later.",
        };
      });
      throw error;
    }
  };

  /**
   * @description fetch the details of a page
   * @param {string} pageId
   */
  fetchPageDetails = async (...args: Parameters<IProjectPageStore["fetchPageDetails"]>) => {
    const [workspaceSlug, projectId, pageId, options] = args;
    const { trackVisit } = options || {};
    try {
      if (!workspaceSlug || !projectId || !pageId) return undefined;

      const currentPageId = this.getPageById(pageId);
      runInAction(() => {
        this.loader = currentPageId ? `mutation-loader` : `init-loader`;
        this.error = undefined;
      });

      const page = await this.service.fetchById(workspaceSlug, projectId, pageId, trackVisit ?? true);

      runInAction(() => {
        if (page?.id) {
          const pageInstance = this.getPageById(page.id);
          if (pageInstance) {
            pageInstance.mutateProperties(page, false);
          } else {
            set(this.data, [page.id], new ProjectPage(this.store, page));
          }
        }
        this.loader = undefined;
      });

      return page;
    } catch (error) {
      runInAction(() => {
        this.loader = undefined;
        this.error = {
          title: "Failed",
          description: "Failed to fetch the page, Please try again later.",
        };
      });
      throw error;
    }
  };

  /**
   * @description create a page
   * @param {Partial<TPage>} pageData
   */
  createPage = async (pageData: Partial<TPage>) => {
    try {
      const { workspaceSlug, projectId } = this.store.router;
      if (!workspaceSlug || !projectId) return undefined;

      runInAction(() => {
        this.loader = "mutation-loader";
        this.error = undefined;
      });

      const page = await this.service.create(workspaceSlug, projectId, {
        ...pageData,
        node_type: pageData.node_type || "page",
      });
      runInAction(() => {
        if (page?.id) set(this.data, [page.id], new ProjectPage(this.store, page));
        this.loader = undefined;
      });

      return page;
    } catch (error) {
      runInAction(() => {
        this.loader = undefined;
        this.error = {
          title: "Failed",
          description: "Failed to create a page, Please try again later.",
        };
      });
      throw error;
    }
  };

  createFolder = async (pageData: Partial<TPage>) =>
    this.createPage({
      ...pageData,
      node_type: "folder",
      name: pageData.name?.trim() || "New folder",
    });

  moveToFolder = async (pageId: string, parentId: string | null) => {
    const { workspaceSlug, projectId } = this.store.router;
    const page = this.getPageById(pageId);
    if (!workspaceSlug || !projectId || !page) throw new Error("Page not found");
    const updated = await this.service.update(workspaceSlug, projectId, pageId, { parent: parentId });
    runInAction(() => {
      page.mutateProperties({ ...updated, parent: parentId });
    });
  };

  /**
   * @description delete a page
   * @param {string} pageId
   */
  removePage = async ({ pageId, shouldSync: _shouldSync = true }: { pageId: string; shouldSync?: boolean }) => {
    try {
      const { workspaceSlug, projectId } = this.store.router;
      if (!workspaceSlug || !projectId || !pageId) return undefined;

      await this.service.remove(workspaceSlug, projectId, pageId);
      runInAction(() => {
        unset(this.data, [pageId]);
        if (this.rootStore.favorite.entityMap[pageId]) this.rootStore.favorite.removeFavoriteFromStore(pageId);
      });
    } catch (error) {
      runInAction(() => {
        this.loader = undefined;
        this.error = {
          title: "Failed",
          description: "Failed to delete a page, Please try again later.",
        };
      });
      throw error;
    }
  };

  /**
   * @description move a page to a new project
   */
  movePage = async (workspaceSlug: string, projectId: string, pageId: string, newProjectId: string) => {
    try {
      await this.service.move(workspaceSlug, projectId, pageId, newProjectId);
      runInAction(() => {
        unset(this.data, [pageId]);
      });
    } catch (error) {
      console.error("Unable to move page", error);
      throw error;
    }
  };
}
