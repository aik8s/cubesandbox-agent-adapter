export async function resolve(specifier, context, nextResolve) {
  if (specifier === "openclaw/plugin-sdk/tool-plugin") {
    return {
      shortCircuit: true,
      url: `data:text/javascript,${encodeURIComponent(`
        export const defineToolPlugin = (value) => {
          const tools = value.tools((definition) => definition);
          return {
            id: value.id,
            register(api) {
              const config = api.pluginConfig || {};
              for (const tool of tools) {
                if (tool.factory) {
                  api.registerTool(
                    (toolContext) => tool.factory({ api, config, toolContext }),
                    { name: tool.name },
                  );
                } else {
                  api.registerTool(tool, { name: tool.name });
                }
              }
            },
          };
        };
      `)}`,
    };
  }
  return nextResolve(specifier, context);
}
