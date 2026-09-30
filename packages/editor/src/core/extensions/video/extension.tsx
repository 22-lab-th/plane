import { useCallback, useEffect, useState } from "react";
import { NodeViewWrapper, ReactNodeViewRenderer } from "@tiptap/react";
import type { NodeViewProps } from "@tiptap/react";
import type { TFileHandler } from "@/types";
import { VideoExtensionConfig } from "./extension-config";

/* oxlint-disable jsx-a11y/media-has-caption -- Imported footage may not include a caption track; do not fabricate captions. */

export const VideoExtension = ({ fileHandler }: { fileHandler: TFileHandler }) => {
  function VideoNodeView({ node }: NodeViewProps) {
    const source = node.attrs.src as string | null;
    const [url, setUrl] = useState("");
    const [error, setError] = useState(false);
    const [refresh, setRefresh] = useState(0);
    const [hasRetried, setHasRetried] = useState(false);
    useEffect(() => {
      let active = true;
      setUrl("");
      setError(false);
      if (source) {
        fileHandler
          .getAssetSrc(source)
          .then((resolved) => {
            if (active) setUrl(resolved);
            return;
          })
          .catch(() => {
            if (active) setError(true);
          });
      } else setError(true);
      return () => {
        active = false;
      };
    }, [source, refresh]);
    const retry = useCallback(() => {
      setHasRetried(false);
      setRefresh((value) => value + 1);
    }, []);
    return (
      <NodeViewWrapper contentEditable={false} className="my-3">
        {error ? (
          <button type="button" onClick={retry}>
            Video could not be loaded. Retry
          </button>
        ) : url ? (
          <video
            controls
            preload="metadata"
            src={url}
            style={{ maxWidth: "100%", width: node.attrs.width || undefined }}
            onError={() => {
              // A page may stay open longer than a signed URL's TTL. Refresh once.
              if (hasRetried) setError(true);
              else {
                setHasRetried(true);
                setRefresh((value) => value + 1);
              }
            }}
          />
        ) : (
          <span>Loading video…</span>
        )}
      </NodeViewWrapper>
    );
  }
  return VideoExtensionConfig.extend({
    addNodeView() {
      return ReactNodeViewRenderer(VideoNodeView);
    },
  });
};
