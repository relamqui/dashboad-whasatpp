// Limpa vazamentos de tools do output do agente
for (const item of $input.all()) {
  const mensagemOriginal = item.json.output;
  
  if (mensagemOriginal && typeof mensagemOriginal === 'string') {
    let cleanText = mensagemOriginal;
    
    // Limpa iterativamente rastros de logs de tools encadeados no formato do n8n
    let replaced = true;
    while (replaced) {
      replaced = false;
      let originalText = cleanText;
      
      // Remove o bloco inicial [Used tools: ... ] ou até a separação do próximo Tool:
      cleanText = cleanText.replace(/^\s*\[Used tools:[\s\S]*?(?:\]\]\]?|\](?=;\s*Tool:)|\](?=\s*Tool:))\s*/i, '');
      // Remove os blocos subsequentes encadeados "; Tool: ... ]" ou "]; Tool: ... ]"
      cleanText = cleanText.replace(/^(?:\];\s*|;\s*)?Tool:[\s\S]*?(?:\]\]\]?|\](?=;\s*Tool:)|\](?=\s*Tool:))\s*/i, '');
      
      if (cleanText !== originalText) {
        replaced = true;
      }
    }
    
    // Fallback: se tiver sobrado algum colchete perdido no início, limpa
    cleanText = cleanText.replace(/^[\];\s]+/, '');

    // Limpezas adicionais (parâmetros vazados, nomes de tools específicos)
    cleanText = cleanText
      .replace(/\{[^{}]*"parameters\d+_Value"[^{}]*\}/g, '')
      .replace(/chamar-?vendee?dor/gi, '')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
      
    item.json.mensagem_limpa = cleanText;
    item.json.output = cleanText;
  }
}

return $input.all();
