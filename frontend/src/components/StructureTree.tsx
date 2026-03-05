import { useState } from "react";
import type { StructureElement } from "../types";

interface TreeNodeProps {
  element: StructureElement;
  depth?: number;
}

const TYPE_ICONS: Record<string, string> = {
  heading: "H",
  paragraph: "P",
  table: "T",
  figure: "F",
  list: "L",
  list_item: "Li",
};

const TYPE_COLORS: Record<string, string> = {
  heading: "bg-info-light text-info",
  paragraph: "bg-paper-warm text-ink-muted",
  table: "bg-success-light text-success",
  figure: "bg-warning-light text-warning",
  list: "bg-accent-light text-accent",
  list_item: "bg-accent-light/60 text-accent",
};

function TreeNode({ element, depth = 0 }: TreeNodeProps) {
  const [expanded, setExpanded] = useState(depth < 2);
  const hasChildren = element.children && element.children.length > 0;
  const icon = TYPE_ICONS[element.type] || element.type.charAt(0).toUpperCase();
  const color = TYPE_COLORS[element.type] || "bg-paper-warm text-ink-muted";

  return (
    <div style={{ paddingLeft: depth > 0 ? 20 : 0 }}>
      <button
        type="button"
        onClick={() => hasChildren && setExpanded(!expanded)}
        className={`
          w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-left
          transition-colors duration-150
          ${hasChildren ? "cursor-pointer hover:bg-paper-warm" : "cursor-default"}
        `}
      >
        {/* Expand/collapse indicator */}
        <span className="w-4 text-center text-xs text-ink-muted">
          {hasChildren ? (expanded ? "▾" : "▸") : ""}
        </span>

        {/* Type badge */}
        <span
          className={`
            w-6 h-6 rounded flex items-center justify-center
            text-[10px] font-bold font-mono shrink-0
            ${color}
          `}
        >
          {icon}
        </span>

        {/* Label */}
        <span className="text-sm text-ink truncate flex-1">
          {element.text || (
            <span className="text-ink-muted italic">
              {element.type}
              {element.level ? ` ${element.level}` : ""}
            </span>
          )}
        </span>
      </button>

      {expanded && hasChildren && (
        <div className="border-l border-ink/6 ml-4">
          {element.children!.map((child, i) => (
            <TreeNode key={i} element={child} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

interface StructureTreeProps {
  elements: StructureElement[];
}

export default function StructureTree({ elements }: StructureTreeProps) {
  if (elements.length === 0) {
    return (
      <div className="text-center py-8 text-sm text-ink-muted">
        No structure elements found.
      </div>
    );
  }

  return (
    <div className="space-y-0.5">
      {elements.map((el, i) => (
        <TreeNode key={i} element={el} />
      ))}
    </div>
  );
}
