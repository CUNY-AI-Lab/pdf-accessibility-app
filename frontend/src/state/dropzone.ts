export type DropzoneState = "idle" | "dragging";
export function dropzoneTransition(state: DropzoneState, event: "enter" | "leave" | "drop"): DropzoneState {
  switch (event) {
    case "enter": return "dragging";
    case "leave":
    case "drop": return "idle";
    default: throw new Error(`Illegal dropzone transition from ${state}`);
  }
}
export function dropzoneMode(state: DropzoneState, disabled: boolean): DropzoneState | "disabled" {
  return disabled ? "disabled" : state;
}
export function acceptPickedFiles(files: File[], disabled: boolean, onFiles: (files: File[]) => void) {
  if (disabled) return;
  const pdfs = files.filter((file) => file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"));
  if (pdfs.length > 0) onFiles(pdfs);
}
