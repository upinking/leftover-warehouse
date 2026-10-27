// 专用于本应用的密钥存取。密钥经进程内存管道传输，不进入命令行或账本。
import Foundation
import Security

func reply(_ object: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: object) {
        FileHandle.standardOutput.write(data)
    }
}
let input = FileHandle.standardInput.readDataToEndOfFile()
guard input.count < 32768,
      let request = try? JSONSerialization.jsonObject(with: input) as? [String: Any],
      let account = request["account"] as? String, account.count == 64,
      let action = request["action"] as? String else { reply(["ok": false]); exit(1) }
let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: "cn.upinking.leftoverwarehouse.ai",
    kSecAttrAccount as String: account]
var status: OSStatus = errSecParam
if action == "set", let value = request["value"] as? String {
    let attributes = [kSecValueData as String: Data(value.utf8)]
    status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
    if status == errSecItemNotFound {
        status = SecItemAdd(query.merging(attributes) { _, new in new } as CFDictionary, nil)
    }
    reply(["ok": status == errSecSuccess])
} else if action == "get" {
    var result: CFTypeRef?
    let requestQuery = query.merging([kSecReturnData as String: true]) { _, new in new }
    status = SecItemCopyMatching(requestQuery as CFDictionary, &result)
    if status == errSecItemNotFound { reply(["ok": true, "value": ""]) }
    else if status == errSecSuccess, let data = result as? Data {
        reply(["ok": true, "value": String(data: data, encoding: .utf8) ?? ""])
    } else { reply(["ok": false]) }
} else if action == "delete" {
    status = SecItemDelete(query as CFDictionary)
    reply(["ok": status == errSecSuccess || status == errSecItemNotFound])
} else { reply(["ok": false]) }
