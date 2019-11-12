#!/bin/bash
function clean 
{
    echo 'cleaning up...'
    rm private.pem
    rm public.pem
}

echo 'generating keys'
openssl genrsa -out private.pem 2048
openssl rsa -in private.pem -outform PEM -pubout -out public.pem
if [ -f .env ]; then
    read -r -p ".env file already exists; Do you want to replace the key values? [y/N] " response
    case "$response" in
        [yY][eE][sS]|[yY]) 
            
            ;;
        *)
            echo 'operation cancelled.'
            clean
            exit 1;
            ;;
    esac
fi

cp .example.env temp.env

echo 'setting up private key...'
PRIV=$(awk '{printf "%s\\\\n", $0}' private.pem)
sed -i '' "s%UBER_PRIV_KEY=%UBER_PRIV_KEY=$PRIV%g" temp.env temp.env

echo 'setting up public key...'
PUB=$(awk '{printf "%s\\\\n", $0}' public.pem)
sed -i '' "s%UBER_PUB_KEY=%UBER_PUB_KEY=$PUB%g" temp.env temp.env
mv temp.env .env
clean