/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useRef, useState } from "react";
// plane ui
import { Button } from "@plane/propel/button";
import { EModalPosition, EModalWidth, Input, ModalCore } from "@plane/ui";

type Props = {
  isOpen: boolean;
  onClose: () => void;
  onSubmit: (name: string) => Promise<void> | void;
  title: string;
  submitLabel?: string;
  submittingLabel?: string;
  initialName?: string;
  placeholder?: string;
};

/**
 * Reusable single-input modal for creating or renaming a page folder.
 * Replaces the old window.prompt flow.
 */
export function FolderNameModal(props: Props) {
  const {
    isOpen,
    onClose,
    onSubmit,
    title,
    submitLabel = "Save",
    submittingLabel = "Saving",
    initialName = "",
    placeholder = "Folder name",
  } = props;
  const [name, setName] = useState(initialName);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    setName(initialName);
    // focus after the modal transition so the caret lands in the field
    const timer = setTimeout(() => inputRef.current?.focus(), 100);
    return () => clearTimeout(timer);
  }, [isOpen, initialName]);

  const handleClose = () => {
    onClose();
    setIsSubmitting(false);
  };

  const handleSubmit = async () => {
    const trimmed = name.trim();
    if (!trimmed || trimmed === initialName.trim()) {
      handleClose();
      return;
    }
    setIsSubmitting(true);
    try {
      await onSubmit(trimmed);
      handleClose();
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <ModalCore isOpen={isOpen} handleClose={handleClose} position={EModalPosition.CENTER} width={EModalWidth.SM}>
      <div>
        <div className="space-y-4 p-5">
          <h3 className="text-18 font-medium text-secondary">{title}</h3>
          <Input
            ref={inputRef}
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void handleSubmit();
              }
            }}
            placeholder={placeholder}
            className="w-full"
          />
        </div>
        <div className="flex items-center justify-end gap-2 border-t-[0.5px] border-subtle px-5 py-4">
          <Button variant="secondary" size="lg" onClick={handleClose}>
            Cancel
          </Button>
          <Button variant="primary" size="lg" loading={isSubmitting} onClick={() => void handleSubmit()}>
            {isSubmitting ? submittingLabel : submitLabel}
          </Button>
        </div>
      </div>
    </ModalCore>
  );
}
