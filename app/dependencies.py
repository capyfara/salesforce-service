from fastapi import Request

def get_settings(request: Request):
    return request.app.state.settings

def get_token_manager(request: Request):
    return request.app.state.token_manager

def get_bulk_client(request: Request):
    return request.app.state.bulk_client

def get_scan_store(request: Request):
    return request.app.state.scan_store