/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// plane imports
import { Button } from "@plane/propel/button";
import { cn } from "@plane/utils";
// helpers
import { FILES_FOCUS_RING, FILES_QUICK_VIEWS, type TFilesQuickView } from "./helpers";

type Props = {
  activeView: TFilesQuickView;
  onSelect: (view: TFilesQuickView) => void;
  className?: string;
};

/**
 * The quick views the listing can express. Rendered as a column where there is
 * room for one and as a horizontal filter bar below that, but always visible:
 * the same four buttons reach the DOM at every breakpoint.
 */
export function FilesQuickViews(props: Props) {
  const { activeView, onSelect, className } = props;

  return (
    <nav aria-label="Quick views" className={cn("flex items-center gap-1 overflow-x-auto p-2", className)}>
      {FILES_QUICK_VIEWS.map((view) => (
        <Button
          key={view.key}
          data-testid={`files-quick-${view.key}`}
          variant={activeView === view.key ? "secondary" : "ghost"}
          size="base"
          aria-pressed={activeView === view.key}
          aria-label={`${view.label} files`}
          className={cn(FILES_FOCUS_RING, "justify-start whitespace-nowrap")}
          onClick={() => onSelect(view.key)}
        >
          {view.label}
        </Button>
      ))}
    </nav>
  );
}
