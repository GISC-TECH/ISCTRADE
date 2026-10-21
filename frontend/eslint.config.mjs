import nextVitals from "eslint-config-next/core-web-vitals";

const config = [
  ...nextVitals,
  {
    // React Compiler advisory rules (eslint-plugin-react-hooks 7). The app does not use the compiler and these
    // patterns (fetch-on-mount loading state, caller-provided deps) are valid React 19; keep them visible as warnings.
    rules: {
      "react-hooks/set-state-in-effect": "warn",
      "react-hooks/use-memo": "warn",
    },
  },
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts", "public/**"] },
];

export default config;
