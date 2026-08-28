import type { Config } from "tailwindcss";

/**
 * The palette carries meaning, not decoration:
 *   steel  - our side of the wire (control plane, things we operate)
 *   zone   - the restricted network we reach into
 *   ok / warn / bad - state, kept separate from the two above
 * The same encoding is used in the architecture diagrams.
 */
export default {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { DEFAULT: "#0d1318", 2: "#161e24", 3: "#1b262e" },
        line: { DEFAULT: "#26333c", soft: "#1f2b33" },
        fg: { DEFAULT: "#dfe8ee", 2: "#a9bcc8", 3: "#7d919e" },
        steel: { DEFAULT: "#79b7d8", deep: "#1f5673", wash: "#12303f" },
        zone: { DEFAULT: "#d8a44e", wash: "#3a2c12" },
        ok: { DEFAULT: "#5fbd8b", wash: "#12301f" },
        warn: { DEFAULT: "#d8a44e", wash: "#332715" },
        bad: { DEFAULT: "#e0705f", wash: "#361a16" },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      fontSize: { "2xs": ["0.6875rem", { lineHeight: "1rem" }] },
    },
  },
  plugins: [],
} satisfies Config;
