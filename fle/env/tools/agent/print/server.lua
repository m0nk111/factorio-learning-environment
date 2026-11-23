FLE = FLE or {}
FLE.actions = FLE.actions or {}
FLE.actions.print = function(message)
    message = dump(message)
    return '"'..message..'"'
end