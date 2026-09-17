import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // DnD-flavored accents on the slate dark base (legacy screens)
        ember: {
          400: "#fbbf24",
          500: "#f59e0b",
          600: "#d97706",
        },
      },
      // Ravenlore type scale — the faces themselves are loaded in app/layout.tsx
      // and the stacks live in app/globals.css (--rl-font-serif / --rl-font-sans),
      // so serif stays available for titles only.
      fontFamily: {
        sans: ["var(--rl-font-sans)"],
        serif: ["var(--rl-font-serif)"],
      },
    },
  },
  plugins: [],
};

export default config;
