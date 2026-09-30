import { Node, mergeAttributes } from "@tiptap/core";

/** Store the durable file reference, never the expiring delivery URL. */
export const VideoExtensionConfig = Node.create({
  name: "videoComponent",
  group: "block",
  atom: true,
  draggable: true,
  addAttributes() {
    return { src: { default: null }, width: { default: null }, height: { default: null } };
  },
  parseHTML() {
    return [{ tag: "video-component" }];
  },
  renderHTML({ HTMLAttributes }) {
    return ["video-component", mergeAttributes(HTMLAttributes)];
  },
});
