/**
 * Background upload (§43) via expo-background-task (BGTaskScheduler on iOS,
 * WorkManager on Android). The OS decides when it runs; the task repairs any
 * interrupted work, then drains within a short time budget.
 *
 * `defineTask` must run at module load, so this file is imported from the root
 * layout (app/_layout.tsx) before any screen renders.
 */
import * as BackgroundTask from "expo-background-task";
import * as TaskManager from "expo-task-manager";
import { getOfflineRuntime } from "./runtime";

export const UPLOAD_TASK = "backoffice.evidence-upload";

/** Stay well inside the time iOS grants a background task. */
const BUDGET_MS = 25_000;
/** Minutes. The OS treats it as a minimum and may run the task much later. */
const MINIMUM_INTERVAL_MIN = 15;

if (!TaskManager.isTaskDefined(UPLOAD_TASK)) {
  TaskManager.defineTask(UPLOAD_TASK, async () => {
    try {
      const { pipeline } = getOfflineRuntime();
      await pipeline.recover();
      await pipeline.drain({ deadline: Date.now() + BUDGET_MS });
      return BackgroundTask.BackgroundTaskResult.Success;
    } catch {
      return BackgroundTask.BackgroundTaskResult.Failed;
    }
  });
}

/** Register once; returns false when the OS has background work disabled for the app. */
export async function registerUploadTask(): Promise<boolean> {
  const status = await BackgroundTask.getStatusAsync();
  if (status !== BackgroundTask.BackgroundTaskStatus.Available) return false;
  if (!(await TaskManager.isTaskRegisteredAsync(UPLOAD_TASK))) {
    await BackgroundTask.registerTaskAsync(UPLOAD_TASK, { minimumInterval: MINIMUM_INTERVAL_MIN });
  }
  return true;
}
