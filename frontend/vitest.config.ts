import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["tests/**/*.spec.ts"],
    exclude: ["tests/e2e/**/*.spec.ts"],
  },
});
