package com.example.barunjari

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URI

internal class SitSenseApi(address: String) {
    private val base = address.trim().trimEnd('/').also {
        val uri = URI(it)
        require(uri.scheme in listOf("http", "https") && !uri.host.isNullOrBlank() &&
            uri.userInfo == null && uri.query == null && uri.fragment == null && uri.path.isNullOrEmpty()) {
            "서버 주소를 http://192.168.1.9:8000 형식으로 입력해 주세요"
        }
    }

    suspend fun request(path: String, body: JSONObject? = null): JSONObject = withContext(Dispatchers.IO) {
        val connection = URI(base + path).toURL().openConnection() as HttpURLConnection
        try {
            connection.connectTimeout = 4000
            connection.readTimeout = 4000
            connection.instanceFollowRedirects = false
            connection.setRequestProperty("Accept", "application/json")
            if (body != null) {
                connection.requestMethod = "POST"
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val result = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (code !in 200..299) {
                val errorCode = runCatching { JSONObject(result).optJSONObject("detail")?.optString("code") }.getOrNull()
                error("서버 응답 $code${errorCode?.let { ": $it" }.orEmpty()}")
            }
            JSONObject(result)
        } finally { connection.disconnect() }
    }

    suspend fun sync() = request("/time/sync", JSONObject().put("app_time", System.currentTimeMillis() / 1000.0).put("timezone", "Asia/Seoul"))
}

internal fun baselineMessage(state: String, reason: String): String = when (state) {
    "waiting_seat" -> "방석에 앉아 주세요"
    "measuring" -> "바른 자세를 유지해 주세요"
    "ready" -> "기준 자세 측정이 완료됐어요"
    "failed" -> when (reason) {
        "pressure_low" -> "방석에 깊숙이 바르게 앉아 다시 측정해 주세요"
        "no_person" -> "카메라에 상체가 보이도록 앉아 주세요"
        "sensor_lost" -> "센서 연결을 확인하고 다시 측정해 주세요"
        "too_much_motion" -> "움직임을 줄이고 다시 측정해 주세요"
        else -> "측정에 실패했어요. 다시 시도해 주세요"
    }
    else -> "기준 자세를 측정해 주세요"
}

internal fun postureName(value: String): String = when(value) {
    "normal" -> "정상 자세"; "none" -> "기울기 없음"; "forward", "forward_head" -> "거북목 자세"
    "cross_left" -> "왼다리 꼬기"; "cross_right" -> "오른다리 꼬기"
    "lean_left" -> "왼쪽 체중 편향"; "lean_right" -> "오른쪽 체중 편향"
    "tilt_left", "left" -> "왼쪽 기울기"; "tilt_right", "right" -> "오른쪽 기울기"
    "empty" -> "자리 비움"; "ok" -> "적정 거리"; "caution" -> "거리 주의"; "warning" -> "너무 가까워요"
    else -> "판정 불가"
}
