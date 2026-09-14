/** Presentation only; command authorization remains on the server. */
export type DeskTask = {
  kind: string;
  allowed_roles?: string[];
  assigned_actor_ids?: string[];
};

export function needsManagerAction(task: DeskTask, actorId: string): boolean {
  if (task.kind === "meeting_availability") return false;
  if (task.allowed_roles?.length && !task.allowed_roles.includes("manager"))
    return false;
  if (
    ["clarification", "appointment_access", "meeting_action"].includes(
      task.kind,
    ) &&
    task.assigned_actor_ids?.length &&
    !task.assigned_actor_ids.includes(actorId)
  )
    return false;
  return true;
}

export function taskActionLabel(kind: string): string {
  if (kind === "quote_approval") return "Review service order";
  if (["verify_completion", "meeting_verify"].includes(kind))
    return "Verify outcome";
  if (kind === "meeting_action") return "Review assigned action";
  if (kind === "meeting_availability") return "View meeting responses";
  if (["clarification", "appointment_access"].includes(kind))
    return "View request";
  return "Review case";
}

const taskHeadings: Record<string, string> = {
  quote_approval: "Service order approval",
  verify_completion: "Outcome verification",
  meeting_action: "Assigned action",
  meeting_availability: "Meeting responses",
  clarification: "Resident clarification",
  appointment_access: "Access confirmation",
};
export const taskHeading = (kind: string) =>
  taskHeadings[kind] || kind.replaceAll("_", " ");
