// Limpa vazamentos de tools do output do agente
for (const item of $input.all()) {
  const mensagemOriginal = item.json.output;
  
  if (mensagemOriginal && typeof mensagemOriginal === 'string') {
    let cleanText = mensagemOriginal;
    
    // Estratégia: encontrar o último "]" que faz parte do bloco de tools
    // e pegar apenas o texto depois dele
    if (cleanText.includes('[Used tools:') || cleanText.includes('Tool:')) {
      // Conta colchetes para achar onde o bloco de tools termina
      let depth = 0;
      let cutIndex = -1;
      
      for (let i = 0; i < cleanText.length; i++) {
        if (cleanText[i] === '[') depth++;
        if (cleanText[i] === ']') {
          depth--;
          if (depth <= 0) {
            cutIndex = i;
            // Continua procurando caso haja outro bloco [Used tools:...] colado
            if (i + 1 < cleanText.length && cleanText[i + 1] === '[') {
              continue;
            }
            // Se o próximo caractere não é outro bloco, para aqui
            break;
          }
        }
      }
      
      if (cutIndex > -1 && cutIndex < cleanText.length - 1) {
        cleanText = cleanText.substring(cutIndex + 1);
      }
    }
    
    // Limpa qualquer lixo residual no início
    cleanText = cleanText
      .replace(/^[\s\];,]+/, '')
      .replace(/chamar-?vendee?dor/gi, '')
      .replace(/\{[^{}]*"parameters\d+_Value"[^{}]*\}/g, '')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
      
    item.json.mensagem_limpa = cleanText;
    item.json.output = cleanText;
  }
}

return $input.all();
