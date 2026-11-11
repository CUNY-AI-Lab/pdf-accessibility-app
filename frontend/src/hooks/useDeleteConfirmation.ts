import { useDeleteJob } from "../api/jobs";
import { canCancelDelete, deleteTransition, type DeleteConfirmation } from "../state/deleteConfirmation";
import { useMachine } from "./useMachine";

export function useDeleteConfirmation(jobId: string, onDeleted?: () => void) {
  const mutation = useDeleteJob();
  const { state, send, read } = useMachine<DeleteConfirmation, Parameters<typeof deleteTransition>[1]>({ status: "closed" }, deleteTransition);
  return {
    open: state.status !== "closed",
    pending: state.status === "deleting",
    error: state.status === "failed" ? state.error : null,
    show: () => { if (read().status === "closed") send({ type: "open" }); },
    cancel: () => { if (canCancelDelete(read())) send({ type: "cancel" }); },
    confirm: async () => {
      if (!canCancelDelete(read())) return;
      send({ type: "confirm" });
      try {
        await mutation.mutateAsync(jobId);
      } catch (error) {
        send({ type: "failed", error: error instanceof Error ? error.message : "Deletion failed" });
        return;
      }
      send({ type: "deleted" });
      onDeleted?.();
    },
  };
}
