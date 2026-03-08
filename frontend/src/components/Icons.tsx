interface IconProps {
  size?: number;
  className?: string;
}

const svgBase = (size: number, className: string | undefined, strokeWidth: number) =>
  ({
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    className,
  });

/** Checkmark polyline (points="20 6 9 17 4 12") */
export function CheckIcon({ size = 16, className }: IconProps) {
  return (
    <svg {...svgBase(size, className, 2.5)}>
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

/** Triangle warning icon with exclamation mark */
export function WarningIcon({ size = 16, className }: IconProps) {
  return (
    <svg {...svgBase(size, className, 2)}>
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}

/** X / close icon (two crossing diagonal lines) */
export function XIcon({ size = 16, className }: IconProps) {
  return (
    <svg {...svgBase(size, className, 2)}>
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

/** Right-pointing arrow (horizontal line + chevron) */
export function ArrowRightIcon({ size = 16, className }: IconProps) {
  return (
    <svg {...svgBase(size, className, 2)}>
      <line x1="5" y1="12" x2="19" y2="12" />
      <polyline points="12 5 19 12 12 19" />
    </svg>
  );
}

/** Left-pointing chevron (points="15 18 9 12 15 6") */
export function ChevronLeftIcon({ size = 16, className }: IconProps) {
  return (
    <svg {...svgBase(size, className, 2)}>
      <polyline points="15 18 9 12 15 6" />
    </svg>
  );
}

/** Right-pointing chevron (points="9 18 15 12 9 6") */
export function ChevronRightIcon({ size = 16, className }: IconProps) {
  return (
    <svg {...svgBase(size, className, 2)}>
      <polyline points="9 18 15 12 9 6" />
    </svg>
  );
}
