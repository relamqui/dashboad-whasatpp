// Limpa vazamentos de tools do output do agente
for (const item of $input.all()) {
  const mensagemOriginal = item.json.output;
  
  if (mensagemOriginal && typeof mensagemOriginal === 'string') {
    let mensagemLimpa = mensagemOriginal
      // Remove blocos [Used tools: ... ] (qualquer formato)
      .replace(/\[Used tools:.*?\]/gs, '')
      // Remove blocos [Tool: ... ] avulsos
      .replace(/\[Tool:.*?\]/gs, '')
      // Remove blocos [Input: ... ] avulsos
      .replace(/\[Input:.*?\]/gs, '')
      // Remove blocos [Result: ... ] avulsos
      .replace(/\[Result:.*?\]/gs, '')
      // Remove qualquer JSON solto { ... }
      .replace(/\{[^{}]*"parameters\d+_Value"[^{}]*\}/g, '')
      // Remove qualquer menção a "chamar-vendendor" ou "chamar-vendedor"
      .replace(/chamar-?vendee?dor/gi, '')
      // Remove linhas vazias extras
      .replace(/\n{3,}/g, '\n\n')
      .trim();
    
    item.json.mensagem_limpa = mensagemLimpa;
    item.json.output = mensagemLimpa;
  }
}

return $input.all();
