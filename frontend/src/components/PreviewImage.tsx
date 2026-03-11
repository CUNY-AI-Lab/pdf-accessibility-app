import { useState } from "react";

interface PreviewImageProps {
  src?: string | null;
  alt: string;
  title: string;
  href?: string | null;
  fallbackText?: string;
  imageClassName?: string;
}

interface PreviewImageAssetProps {
  src?: string | null;
  alt: string;
  directHref: string | null;
  fallbackText: string;
  imageClassName: string;
}

function PreviewImageAsset({
  src,
  alt,
  directHref,
  fallbackText,
  imageClassName,
}: PreviewImageAssetProps) {
  const [loadState, setLoadState] = useState<"loading" | "loaded" | "error">(
    src ? "loading" : "error",
  );

  if (!src || loadState === "error") {
    return (
      <div className="rounded-lg border border-ink/8 bg-white/70 p-3">
        <p className="text-xs text-ink-muted">{fallbackText}</p>
        {directHref && (
          <a
            href={directHref}
            target="_blank"
            rel="noreferrer"
            className="mt-2 inline-flex text-xs font-medium text-accent no-underline hover:underline"
          >
            Open direct preview
          </a>
        )}
      </div>
    );
  }

  const image = (
    <div className="relative">
      {loadState === "loading" && (
        <div className="absolute inset-0 animate-pulse rounded-md border border-ink/6 bg-paper-warm/70" />
      )}
      <img
        src={src}
        alt={alt}
        loading="lazy"
        onLoad={() => setLoadState("loaded")}
        onError={() => setLoadState("error")}
        className={imageClassName}
      />
    </div>
  );

  if (directHref) {
    return (
      <a
        href={directHref}
        target="_blank"
        rel="noreferrer"
        className="block"
      >
        {image}
      </a>
    );
  }

  return image;
}

export default function PreviewImage({
  src,
  alt,
  title,
  href,
  fallbackText = "Preview unavailable right now. Open the direct preview link and try again.",
  imageClassName = "w-full rounded-md border border-ink/6 bg-paper-warm object-cover",
}: PreviewImageProps) {
  const directHref = href ?? src ?? null;

  const body = (
    <>
      <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted mb-2">
        {title}
      </p>
      <PreviewImageAsset
        key={src ?? "missing-preview"}
        src={src}
        alt={alt}
        directHref={directHref}
        fallbackText={fallbackText}
        imageClassName={imageClassName}
      />
    </>
  );

  return <div className="rounded-lg border border-ink/8 bg-paper-warm/60 p-2">{body}</div>;
}
