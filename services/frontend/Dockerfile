FROM nginx:1.31-alpine

# Replace the stock config: unprivileged port, temp files in /tmp, so nginx runs as non-root.
COPY nginx.conf /etc/nginx/nginx.conf
RUN rm -f /etc/nginx/conf.d/default.conf
COPY static/ /usr/share/nginx/html/

USER nginx
EXPOSE 8080
