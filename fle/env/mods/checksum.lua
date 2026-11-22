FLE = FLE or {}

if not storage.__lua_script_checksums then
    storage.__lua_script_checksums = {}
end

FLE.get_lua_script_checksums = function()
    return helpers.table_to_json(storage.__lua_script_checksums)
end

FLE.set_lua_script_checksum = function(name, checksum)
    storage.__lua_script_checksums[name] = checksum
end

FLE.clear_lua_script_checksums = function()
    storage.__lua_script_checksums = {}
end