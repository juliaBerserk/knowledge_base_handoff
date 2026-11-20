server_tokens off;
root /usr/share/nginx/html;
client_max_body_size 21m;
resolver 127.0.0.11 valid=10s ipv6=off;
set $estafeta_backend http://backend:8000;

add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
add_header Referrer-Policy no-referrer always;
add_header Content-Security-Policy "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'" always;

location /api/ {
    proxy_pass $estafeta_backend;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 120s;

    location /api/auth/ {
        client_max_body_size 8k;
        proxy_pass $estafeta_backend;
    }
    location /api/materials {
        proxy_request_buffering off;
        proxy_pass $estafeta_backend;
    }
    location /api/chat {
        client_max_body_size 16k;
        proxy_pass $estafeta_backend;
    }
    location = /api/search {
        client_max_body_size 16k;
        proxy_pass $estafeta_backend;
    }
    location /api/admin/ {
        client_max_body_size 512k;
        proxy_pass $estafeta_backend;
    }
}

location = /index.html {
    expires -1;
}

location /assets/ {
    expires 1y;
    try_files $uri =404;
}

location / {
    try_files $uri $uri/ /index.html;
}
