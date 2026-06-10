# Use uma imagem oficial do Python como base
FROM python:3.11-slim

# Define o diretório de trabalho no container
WORKDIR /app

# Copia os arquivos de dependências
COPY requirements.txt .

# Instala as dependências
RUN pip install --no-cache-dir -r requirements.txt

# Copia todo o projeto para o container
COPY . .

# Cria o diretório de dados persistentes
RUN mkdir -p /app/data

# Expõe a porta que o sistema usa
EXPOSE 3008

# Define variáveis de ambiente padrão
ENV PORT=3008
ENV DB_PATH=/app/data/db.json
ENV TZ=America/Sao_Paulo

# Adiciona permissão de execução ao script de inicialização
RUN chmod +x start.sh

# Comando para rodar a aplicação via script
CMD ["./start.sh"]
