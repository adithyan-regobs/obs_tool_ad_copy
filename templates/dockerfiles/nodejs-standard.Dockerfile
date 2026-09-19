FROM node:{{NODE_VERSION}}-alpine

WORKDIR /app

{{CUSTOM_BUILD_ARGS}}
# Copy package files first for better caching
COPY package*.json ./

# Install dependencies
RUN npm ci --only=production

# Copy application code
COPY . .

ENV PORT={{PORT}}
EXPOSE {{PORT}}

CMD ["node", "index.js"]
